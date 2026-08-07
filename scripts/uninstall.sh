#!/bin/sh
set -eu
umask 077

LC_ALL=C
export LC_ALL

fail() {
    printf 'goalrouter uninstall: %s\n' "$*" >&2
    exit 1
}

valid_text() {
    [ -n "$1" ] || return 1
    text_hex=$(printf '%s' "$1" | od -An -v -t x1) || return 1
    # Word splitting is intentional: od emits whitespace-separated bytes.
    # shellcheck disable=SC2086
    set -- $text_hex
    [ "$#" -gt 0 ] || return 1
    for text_byte do
        case $text_byte in
            20 | 2[1-9a-f] | [3-6][0-9a-f] | 7[0-9a-e]) ;;
            *) return 1 ;;
        esac
    done
}

valid_path() {
    valid_text "$1" || return 1
    case $1 in
        /*) ;;
        *) return 1 ;;
    esac
    case $1/ in
        *'/../'* | *'/./'* | *'//'*) return 1 ;;
    esac
}

paths_overlap() {
    [ "$1" = "$2" ] || [ "${1#"$2"/}" != "$1" ] || [ "${2#"$1"/}" != "$2" ]
}

read_owned() {
    owned_name=$1
    owned_file=$control_dir/$owned_name
    [ -f "$owned_file" ] && [ ! -L "$owned_file" ] \
        || fail "missing or unsafe ownership field: $owned_name"
    owned_value=$(cat "$owned_file") || fail "cannot read ownership field: $owned_name"
    valid_text "$owned_value" || fail "invalid ownership field: $owned_name"
}

resolve_exact_directory() {
    exact_candidate=$1
    exact_label=$2
    valid_path "$exact_candidate" || fail "invalid $exact_label path"
    [ -d "$exact_candidate" ] && [ ! -L "$exact_candidate" ] \
        || fail "$exact_label is missing or is a symbolic link"
    exact_physical=$(CDPATH='' cd -P "$exact_candidate" && pwd -P) \
        || fail "cannot resolve $exact_label"
    [ "$exact_physical" = "$exact_candidate" ] \
        || fail "$exact_label contains a symbolic-link escape"
}

purge_tree() {
    purge_target=$1
    purge_sentinel=$purge_target/.goalrouter-owned-v1
    find "$purge_target" -depth \
        ! -path "$purge_target" \
        ! -path "$purge_sentinel" \
        -exec sh -c '
        set -eu
        for purge_item do
            if [ -d "$purge_item" ] && [ ! -L "$purge_item" ]; then
                rmdir "$purge_item"
            else
                rm -f "$purge_item"
            fi
        done
    ' sh {} +
    purge_remaining=$(find "$purge_target" -mindepth 1 \
        ! -path "$purge_sentinel" -print -quit) \
        || return 1
    [ -z "$purge_remaining" ] || return 1

    # The authorization sentinel remains throughout fallible traversal. Once
    # it is the only entry, make its unlink plus root removal a bounded final
    # section and restore recovery traps before the next purge phase.
    trap '' HUP INT TERM
    purge_final_status=0
    rm -f "$purge_sentinel" || purge_final_status=$?
    if [ "$purge_final_status" -eq 0 ]; then
        rmdir "$purge_target" || purge_final_status=$?
    fi
    trap 'interrupted 129' HUP
    trap 'interrupted 130' INT
    trap 'interrupted 143' TERM
    [ "$purge_final_status" -eq 0 ]
}

state_dir=
config_dir=
purge=0
confirmed=0
seen_state_dir=0
seen_config_dir=0
seen_purge=0
seen_yes=0
while [ "$#" -gt 0 ]; do
    case $1 in
        --state-dir | --config-dir)
            uninstall_option=$1
            [ "$#" -ge 2 ] || fail "$uninstall_option requires a value"
            if [ "$uninstall_option" = --state-dir ]; then
                [ "$seen_state_dir" -eq 0 ] || fail 'duplicate option: --state-dir'
                seen_state_dir=1
                state_dir=$2
            else
                [ "$seen_config_dir" -eq 0 ] || fail 'duplicate option: --config-dir'
                seen_config_dir=1
                config_dir=$2
            fi
            shift 2
            ;;
        --purge)
            [ "$seen_purge" -eq 0 ] || fail 'duplicate option: --purge'
            seen_purge=1; purge=1; shift
            ;;
        --yes)
            [ "$seen_yes" -eq 0 ] || fail 'duplicate option: --yes'
            seen_yes=1; confirmed=1; shift
            ;;
        --help)
            printf '%s\n' 'Usage: uninstall.sh [--state-dir <absolute-path>] [--config-dir <absolute-path>] [--purge] [--yes]'
            exit 0
            ;;
        *) fail "unknown option: $1" ;;
    esac
done

[ -n "${HOME:-}" ] || fail 'HOME is required'
valid_path "$HOME" || fail 'HOME must be an absolute printable path'
[ "$HOME" != / ] || fail 'HOME cannot be root'
state_root=${XDG_STATE_HOME:-"$HOME/.local/state"}
config_root=${XDG_CONFIG_HOME:-"$HOME/.config"}

uninstaller_self=$0
case $uninstaller_self in
    /*) ;;
    */*) uninstaller_self=$(pwd -P)/$uninstaller_self ;;
    *) uninstaller_self=$(command -v "$uninstaller_self") || fail 'cannot resolve installed uninstaller path' ;;
esac
[ -f "$uninstaller_self" ] && [ -x "$uninstaller_self" ] && [ ! -L "$uninstaller_self" ] \
    || fail 'installed uninstaller path is missing or unsafe'
uninstaller_name=${uninstaller_self##*/}
bin_parent=${uninstaller_self%/*}
trusted_bin=$(CDPATH='' cd -P "$bin_parent" && pwd -P) \
    || fail 'cannot resolve installed lifecycle directory'
[ "$uninstaller_name" = goalrouter-uninstall ] \
    || fail 'installed uninstaller must use its physical canonical path'
uninstaller_self=$trusted_bin/$uninstaller_name
control_dir=$trusted_bin/.goalrouter-control
resolve_exact_directory "$control_dir" 'trusted control directory'

for required_tool in awk cat find mktemp mv od rm rmdir sh; do
    command -v "$required_tool" >/dev/null 2>&1 \
        || fail "required tool is unavailable: $required_tool"
done
if command -v sha256sum >/dev/null 2>&1; then
    hash_tool=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    hash_tool=shasum
else
    fail 'required checksum tool is unavailable: sha256sum or shasum'
fi
[ -f "$control_dir/ownership.sha256" ] && [ ! -L "$control_dir/ownership.sha256" ] \
    || fail 'missing or unsafe trusted control manifest'
expected_manifest_names='install.json
image-ref
image-digest
launcher-version
protocol-version
app-version
image-platform
source-revision
release-base
release-transport
image-repository
owned-home
owned-bin-dir
owned-config-dir
owned-state-dir
owned-codex-home
owned-launcher
owned-installer
owned-uninstaller
guard-config-root
guard-state-root
guard-config-parent
guard-state-parent
guard-bin-parent'
manifest_names=$(awk '{ if (NF != 2 || $1 !~ /^[0-9a-f]{64}$/) exit 2; print $2 }' \
    "$control_dir/ownership.sha256") || fail 'corrupt trusted control manifest'
[ "$manifest_names" = "$expected_manifest_names" ] \
    || fail 'ownership manifest has missing, duplicate, or unexpected entries'
if [ "$hash_tool" = sha256sum ]; then
    (CDPATH='' cd "$control_dir" && sha256sum -c ownership.sha256) >/dev/null 2>&1 \
        || fail 'trusted control checksum mismatch'
else
    (CDPATH='' cd "$control_dir" && shasum -a 256 -c ownership.sha256) >/dev/null 2>&1 \
        || fail 'trusted control checksum mismatch'
fi
control_entries=$(find "$control_dir" -mindepth 1 -maxdepth 1 -print) \
    || fail 'cannot inspect trusted control entries'
while IFS= read -r control_entry; do
    [ -n "$control_entry" ] || continue
    control_name=${control_entry##*/}
    control_allowed=0
    for expected_name in $expected_manifest_names ownership.sha256; do
        if [ "$control_name" = "$expected_name" ]; then
            control_allowed=1
            break
        fi
    done
    if [ "$control_name" = .uninstalling ]; then
        control_allowed=1
    fi
    case $control_name in
        .uninstalling.*)
            [ -f "$control_entry" ] && [ ! -L "$control_entry" ] \
                || fail 'unsafe staged uninstall recovery marker'
            rm -f "$control_entry" \
                || fail 'cannot clean staged uninstall recovery marker'
            control_allowed=1
            ;;
    esac
    [ "$control_allowed" -eq 1 ] || fail 'unexpected trusted control entry'
done <<EOF
$control_entries
EOF
read_owned owned-state-dir; trusted_state_dir=$owned_value
read_owned owned-config-dir; trusted_config_dir=$owned_value
state_dir=${state_dir:-$trusted_state_dir}
config_dir=${config_dir:-$trusted_config_dir}
uninstall_marker=$control_dir/.uninstalling
resume=0
if [ -e "$uninstall_marker" ] || [ -L "$uninstall_marker" ]; then
    [ -f "$uninstall_marker" ] && [ ! -L "$uninstall_marker" ] \
        || fail 'unsafe uninstall recovery marker'
    marker_mode=$(cat "$uninstall_marker") || fail 'cannot read uninstall recovery marker'
    case $marker_mode in preserve | purge) ;; *) fail 'invalid uninstall recovery marker' ;; esac
    requested_mode=preserve; [ "$purge" -eq 0 ] || requested_mode=purge
    [ "$marker_mode" = "$requested_mode" ] \
        || fail 'retry must use the original uninstall mode'
    resume=1
fi
[ "$state_dir" = "$trusted_state_dir" ] \
    || fail 'requested state directory is not installer-owned'
[ "$config_dir" = "$trusted_config_dir" ] \
    || fail 'requested config directory is not installer-owned'
marker_temp=
final_safe=0
interrupted() {
    interrupted_status=$1
    trap - HUP INT TERM
    if [ -n "$marker_temp" ]; then
        case $marker_temp in
            "$control_dir/.uninstalling."*)
                [ -L "$marker_temp" ] || rm -f "$marker_temp" 2>/dev/null || :
                ;;
        esac
    fi
    if [ "$final_safe" -eq 0 ]; then
        if [ "$purge" -eq 1 ]; then
            retry_mode='--purge and --yes'
        else
            retry_mode='--yes'
        fi
        printf 'goalrouter uninstall: interrupted; retry with %s using %s\n' \
            "$uninstaller_self" "$retry_mode" >&2
    fi
    exit "$interrupted_status"
}
trap 'interrupted 129' HUP
trap 'interrupted 130' INT
trap 'interrupted 143' TERM

if [ "$resume" -eq 0 ]; then
    resolve_exact_directory "$state_dir" 'state directory'
    resolve_exact_directory "$config_dir" 'config directory'
    [ -f "$state_dir/ownership.sha256" ] && [ ! -L "$state_dir/ownership.sha256" ] \
        || fail 'missing or unsafe runtime ownership manifest'
    [ "$(cat "$state_dir/ownership.sha256")" = "$(cat "$control_dir/ownership.sha256")" ] \
        || fail 'runtime state metadata does not match trusted installation control'
    if [ "$hash_tool" = sha256sum ]; then
        (CDPATH='' cd "$state_dir" && sha256sum -c ownership.sha256) >/dev/null 2>&1 \
            || fail 'runtime ownership metadata checksum mismatch'
    else
        (CDPATH='' cd "$state_dir" && shasum -a 256 -c ownership.sha256) >/dev/null 2>&1 \
            || fail 'runtime ownership metadata checksum mismatch'
    fi
fi

read_owned owned-home; owned_home=$owned_value
read_owned owned-bin-dir; owned_bin_dir=$owned_value
read_owned owned-config-dir; owned_config_dir=$owned_value
read_owned owned-state-dir; owned_state_dir=$owned_value
read_owned owned-launcher; owned_launcher=$owned_value
read_owned owned-installer; owned_installer=$owned_value
read_owned owned-uninstaller; owned_uninstaller=$owned_value
read_owned guard-config-root; guard_config_root=$owned_value
read_owned guard-state-root; guard_state_root=$owned_value
read_owned guard-config-parent; guard_config_parent=$owned_value
read_owned guard-state-parent; guard_state_parent=$owned_value
read_owned guard-bin-parent; guard_bin_parent=$owned_value

[ "$owned_home" = "$HOME" ] || fail 'current HOME does not match installation ownership'
[ "$owned_state_dir" = "$state_dir" ] || fail 'requested state directory is not installer-owned'
[ "$owned_config_dir" = "$config_dir" ] || fail 'requested config directory is not installer-owned'
for owned_path in "$owned_bin_dir" "$owned_config_dir" "$owned_state_dir" "$owned_launcher" "$owned_installer" "$owned_uninstaller" "$guard_config_root" "$guard_state_root" "$guard_config_parent" "$guard_state_parent" "$guard_bin_parent"; do
    valid_path "$owned_path" || fail 'ownership metadata contains an unsafe path'
done
[ "$owned_launcher" = "$owned_bin_dir/goalrouter" ] \
    && [ "$owned_installer" = "$owned_bin_dir/goalrouter-install" ] \
    && [ "$owned_uninstaller" = "$owned_bin_dir/goalrouter-uninstall" ] \
    || fail 'owned lifecycle paths are inconsistent'
[ "$owned_bin_dir" = "$trusted_bin" ] || fail 'trusted bin ownership is inconsistent'
[ "$guard_config_parent" = "${owned_config_dir%/*}" ] \
    || fail 'owned config parent is inconsistent'
[ "$guard_state_parent" = "${owned_state_dir%/*}" ] \
    || fail 'owned state parent is inconsistent'
[ "$guard_bin_parent" = "${owned_bin_dir%/*}" ] \
    || fail 'owned bin parent is inconsistent'

for lifecycle_file in "$owned_launcher" "$owned_installer" "$owned_uninstaller"; do
    [ ! -L "$lifecycle_file" ] || fail 'owned lifecycle file became a symbolic link'
    [ ! -e "$lifecycle_file" ] || [ -f "$lifecycle_file" ] \
        || fail 'owned lifecycle path is not a regular file'
done

if [ "$purge" -eq 1 ]; then
    [ "$guard_config_root" = "$config_root" ] || fail 'XDG config ownership is inconsistent'
    [ "$guard_state_root" = "$state_root" ] || fail 'XDG state ownership is inconsistent'
    for sentinel_dir in "$owned_config_dir" "$owned_state_dir"; do
        if [ ! -e "$sentinel_dir" ] && [ ! -L "$sentinel_dir" ] \
            && [ "$resume" -eq 1 ]; then
            continue
        fi
        resolve_exact_directory "$sentinel_dir" 'owned purge directory'
        sentinel=$sentinel_dir/.goalrouter-owned-v1
        if [ -f "$sentinel" ] && [ ! -L "$sentinel" ]; then
            [ "$(cat "$sentinel")" = goalrouter-owned-directory-v1 ] \
                || fail 'refusing purge with an invalid directory ownership sentinel'
        elif [ ! -e "$sentinel" ] && [ ! -L "$sentinel" ] \
            && [ "$resume" -eq 1 ] && [ "$marker_mode" = purge ]; then
            sentinel_missing_entry=$(find "$sentinel_dir" -mindepth 1 -print -quit) \
                || fail 'cannot inspect sentinel-missing purge recovery target'
            [ -z "$sentinel_missing_entry" ] \
                || fail 'refusing nonempty purge target without an ownership sentinel'
        else
            fail 'refusing purge without an exact directory ownership sentinel'
        fi
    done
    for purge_target in "$owned_config_dir" "$owned_state_dir"; do
        [ "$purge_target" != / ] || fail 'refusing to purge root'
        [ "$purge_target" != "$owned_home" ] || fail 'refusing to purge HOME'
        [ "$purge_target" != "$guard_config_root" ] || fail 'refusing to purge XDG config root'
        [ "$purge_target" != "$guard_state_root" ] || fail 'refusing to purge XDG state root'
        [ "$purge_target" != "$guard_config_parent" ] || fail 'refusing to purge config parent'
        [ "$purge_target" != "$guard_state_parent" ] || fail 'refusing to purge state parent'
        [ "$purge_target" != "$owned_bin_dir" ] || fail 'refusing to purge bin directory'
        [ "$purge_target" != "$guard_bin_parent" ] || fail 'refusing to purge bin parent'
        [ "$purge_target" != "${owned_config_dir%/*}" ] || fail 'refusing broad config purge'
        [ "$purge_target" != "${owned_state_dir%/*}" ] || fail 'refusing broad state purge'
    done
    paths_overlap "$owned_config_dir" "$owned_state_dir" \
        && fail 'refusing overlapping purge targets'
fi

if [ "$confirmed" -eq 0 ]; then
    [ -t 0 ] || fail 'confirmation required; use --yes for non-interactive uninstall'
    printf 'Uninstall GoalRouter? [y/N] ' >&2
    IFS= read -r answer || fail 'confirmation failed'
    [ "$answer" = y ] || [ "$answer" = Y ] || fail 'uninstall cancelled'
fi

if [ "$resume" -eq 0 ]; then
    marker_mode=preserve; [ "$purge" -eq 0 ] || marker_mode=purge
    marker_temp=$(mktemp "$control_dir/.uninstalling.XXXXXXXX") \
        || fail 'cannot create uninstall recovery marker'
    [ -f "$marker_temp" ] && [ ! -L "$marker_temp" ] \
        || fail 'unsafe uninstall recovery marker staging file'
    printf '%s' "$marker_mode" >"$marker_temp" \
        || fail 'cannot write uninstall recovery marker'
    mv "$marker_temp" "$uninstall_marker" \
        || fail 'cannot activate uninstall recovery marker'
    marker_temp=
fi

if [ "$purge" -eq 1 ]; then
    if [ -e "$owned_config_dir" ] || [ -L "$owned_config_dir" ]; then
        purge_tree "$owned_config_dir"
    fi
    if [ -e "$owned_state_dir" ] || [ -L "$owned_state_dir" ]; then
        purge_tree "$owned_state_dir"
    fi
else
    rm -f \
        "$state_dir/image-ref" \
        "$state_dir/image-digest" \
        "$state_dir/launcher-version" \
        "$state_dir/protocol-version" \
        "$state_dir/app-version" \
        "$state_dir/image-platform" \
        "$state_dir/source-revision" \
        "$state_dir/release-base" \
        "$state_dir/release-transport" \
        "$state_dir/image-repository" \
        "$state_dir/install.json" \
        "$state_dir/owned-home" \
        "$state_dir/owned-bin-dir" \
        "$state_dir/owned-config-dir" \
        "$state_dir/owned-state-dir" \
        "$state_dir/owned-codex-home" \
        "$state_dir/owned-launcher" \
        "$state_dir/owned-installer" \
        "$state_dir/owned-uninstaller" \
        "$state_dir/guard-config-root" \
        "$state_dir/guard-state-root" \
        "$state_dir/guard-config-parent" \
        "$state_dir/guard-state-parent" \
        "$state_dir/guard-bin-parent" \
        "$state_dir/ownership.sha256"
fi
rm -f "$owned_launcher"
rm -f "$owned_installer"
# Until this point the physical uninstaller and its checksummed control root
# remain available, so any interruption is safely retryable.
final_safe=1
# The remaining self/control cleanup is the final safe point. Ignore lifecycle
# signals for this bounded section so it cannot strand an untrusted orphan.
trap '' HUP INT TERM
rm -f "$owned_uninstaller"
# The trusted control root is removed only after every field has been read and
# every authorized product target has been processed.
for control_name in $expected_manifest_names ownership.sha256; do
    rm -f "$control_dir/$control_name"
done
rm -f "$uninstall_marker"
rmdir "$control_dir"
if [ "$purge" -eq 1 ]; then
    printf 'GoalRouter removed; exact recorded configuration and state were purged.\n'
else
    printf 'GoalRouter removed; configuration and durable state were preserved.\n'
fi
