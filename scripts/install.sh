#!/bin/sh
set -eu
umask 077

LC_ALL=C
export LC_ALL

fail() {
    printf 'goalrouter install: %s\n' "$*" >&2
    exit 1
}

require_tool() {
    command -v "$1" >/dev/null 2>&1 || fail "required tool is unavailable: $1"
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

check_destination() {
    destination=$1
    destination_label=$2
    valid_path "$destination" || fail "invalid $destination_label path"
    [ "$destination" != / ] || fail "$destination_label cannot be root"
    [ "$destination" != "$HOME" ] || fail "$destination_label cannot be HOME"
    [ ! -L "$destination" ] || fail "$destination_label cannot be a symbolic link"
    ancestor=$destination
    while [ ! -e "$ancestor" ]; do
        next_ancestor=${ancestor%/*}
        [ -n "$next_ancestor" ] || next_ancestor=/
        [ "$next_ancestor" != "$ancestor" ] || fail "cannot resolve $destination_label parent"
        ancestor=$next_ancestor
    done
    [ -d "$ancestor" ] || fail "$destination_label parent is not a directory: $ancestor"
    [ ! -L "$ancestor" ] || fail "$destination_label parent cannot be a symbolic link"
    [ -w "$ancestor" ] || fail "$destination_label parent is not writable: $ancestor"
    physical_ancestor=$(CDPATH='' cd -P "$ancestor" && pwd -P) \
        || fail "cannot resolve $destination_label parent: $ancestor"
    [ "$physical_ancestor" = "$ancestor" ] \
        || fail "$destination_label parent contains a symbolic-link escape"
    owned_ancestor=$(find "$ancestor" -prune -user "$install_uid" -print) \
        || fail "cannot inspect $destination_label parent ownership"
    [ "$owned_ancestor" = "$ancestor" ] \
        || fail "$destination_label parent is not owned by the invoking user"
    shared_writable=$(find "$ancestor" -prune -perm -0022 -print) \
        || fail "cannot inspect $destination_label parent mode"
    [ -z "$shared_writable" ] \
        || fail "$destination_label parent cannot be group/world writable"
}

valid_digest() {
    case $1 in
        sha256:*) digest_hex=${1#sha256:} ;;
        *) return 1 ;;
    esac
    [ "${#digest_hex}" -eq 64 ] || return 1
    case $digest_hex in
        *[!0-9a-f]*) return 1 ;;
    esac
}

valid_version() {
    printf '%s\n' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'
}

version_at_least() {
    awk -v actual="$1" -v minimum="$2" 'BEGIN {
        actual_count = split(actual, actual_part, ".")
        minimum_count = split(minimum, minimum_part, ".")
        if (actual_count < 2 || actual_count > 3 || minimum_count < 2 || minimum_count > 3) exit 2
        for (i = 1; i <= actual_count; i += 1) if (actual_part[i] !~ /^[0-9]+$/) exit 2
        for (i = 1; i <= minimum_count; i += 1) if (minimum_part[i] !~ /^[0-9]+$/) exit 2
        for (i = 1; i <= 3; i += 1) {
            actual_value = (i <= actual_count) ? actual_part[i] + 0 : 0
            minimum_value = (i <= minimum_count) ? minimum_part[i] + 0 : 0
            if (actual_value > minimum_value) exit 0
            if (actual_value < minimum_value) exit 1
        }
        exit 0
    }'
}

valid_image() {
    valid_text "$1" || return 1
    case $1 in
        -* | *@* | *[[:space:]]*) return 1 ;;
        */*) ;;
        *) return 1 ;;
    esac
    image_last=${1##*/}
    case $image_last in
        *:*)
            image_tag=${image_last##*:}
            [ -n "$image_tag" ] || return 1
            image_repository=${1%:*}
            ;;
        *) return 1 ;;
    esac
    case $image_repository in
        '' | -* | *[!A-Za-z0-9._:/-]*) return 1 ;;
    esac
}

valid_https_release_base() {
    valid_text "$1" || return 1
    case $1 in
        https://*) ;;
        *) return 1 ;;
    esac
    case $1 in
        *' '* | *'?'* | *'#'* | *'@'* | *\\*) return 1 ;;
    esac
    https_remainder=${1#https://}
    https_authority=${https_remainder%%/*}
    [ -n "$https_authority" ] || return 1
    https_domain_component='([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])'
    printf '%s\n' "$https_authority" \
        | grep -Eq "^$https_domain_component([.]$https_domain_component)*(:[0-9]+)?$|^\\[[A-Fa-f0-9:]+\\](:[0-9]+)?$"
}

download() {
    download_url=$1
    download_target=$2
    if [ "$transport" = https ]; then
        curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --silent --show-error \
            --output "$download_target" "$download_url" \
            || fail 'download failed for release asset'
    else
        curl --fail --proto '=http' --silent --show-error \
            --output "$download_target" "$download_url" \
            || fail 'loopback download failed for release asset'
    fi
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

write_value() {
    printf '%s' "$2" >"$metadata_stage/$1"
}

rollback_transaction() {
    [ -n "${rollback_file:-}" ] && [ -f "$rollback_file" ] || return 0
    if [ -n "${rollback_temp_file:-}" ] && [ -f "$rollback_temp_file" ]; then
        while IFS= read -r rollback_temp; do
            [ -n "$rollback_temp" ] || continue
            rm -f "$rollback_temp" 2>/dev/null || :
        done <"$rollback_temp_file"
    fi
    while IFS=' ' read -r rollback_kind rollback_target; do
        [ -n "$rollback_kind" ] && [ -n "$rollback_target" ] || continue
        rollback_backup=$rollback_target.goalrouter-backup.$$
        if [ "$rollback_kind" = existing ] && [ -e "$rollback_backup" ]; then
            rm -f "$rollback_target" 2>/dev/null || :
            mv "$rollback_backup" "$rollback_target" 2>/dev/null || :
        elif [ "$rollback_kind" = new ]; then
            rm -f "$rollback_target" 2>/dev/null || :
        fi
    done <"$rollback_file"
    rmdir "$control_dir" "$config_dir" "$state_dir" "$bin_dir" 2>/dev/null || :
}

cleanup() {
    cleanup_status=$?
    trap - EXIT HUP INT TERM
    if [ "${transaction_active:-0}" -eq 1 ]; then
        rollback_transaction
    fi
    if [ -n "${work_dir:-}" ] && [ -d "$work_dir" ]; then
        rm -rf "$work_dir"
    fi
    exit "$cleanup_status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

version=latest
release_base=
release_base_explicit=0
allow_loopback_http=0
image=
image_repository_option=ghcr.io/vparla/goalrouter
bin_dir=
config_dir=
state_dir=
codex_home=
confirmed=0
reset_config=0
force_repair=0
skip_doctor=0
path_hint=1

while [ "$#" -gt 0 ]; do
    case $1 in
        --version | --release-base | --image | --image-repository | --bin-dir | --config-dir | --state-dir | --codex-home)
            option=$1
            [ "$#" -ge 2 ] || fail "$option requires a value"
            value=$2
            shift 2
            case $option in
                --version) version=$value ;;
                --release-base)
                    release_base=$value
                    release_base_explicit=1
                    ;;
                --image) image=$value ;;
                --image-repository) image_repository_option=$value ;;
                --bin-dir) bin_dir=$value ;;
                --config-dir) config_dir=$value ;;
                --state-dir) state_dir=$value ;;
                --codex-home) codex_home=$value ;;
            esac
            ;;
        --allow-loopback-http) allow_loopback_http=1; shift ;;
        --yes) confirmed=1; shift ;;
        --force)
            force_repair=1
            reset_config=1
            shift
            ;;
        --reset-config) reset_config=1; shift ;;
        --skip-doctor) skip_doctor=1; shift ;;
        --no-path-hint) path_hint=0; shift ;;
        --help)
            cat <<'EOF'
Usage: install.sh [options]
  --version <x.y.z|latest>
  --bin-dir <absolute-path>
  --config-dir <absolute-path>
  --state-dir <absolute-path>
  --codex-home <absolute-path>
  --release-base <https-url>
  --image <registry/repository:tag>
  --image-repository <registry/repository>
  --allow-loopback-http
  --reset-config | --force
  --skip-doctor
  --no-path-hint
  --yes
EOF
            exit 0
            ;;
        *) fail "unknown option: $1" ;;
    esac
done

[ -n "${HOME:-}" ] || fail 'HOME is required'
valid_path "$HOME" || fail 'HOME must be an absolute printable path'
[ "$HOME" != / ] || fail 'HOME cannot be root'
[ -d "$HOME" ] || fail 'HOME does not exist'
[ ! -L "$HOME" ] || fail 'HOME cannot be a symbolic link'

config_root=${XDG_CONFIG_HOME:-"$HOME/.config"}
state_root=${XDG_STATE_HOME:-"$HOME/.local/state"}
bin_root=${XDG_BIN_HOME:-"$HOME/.local/bin"}
bin_dir=${bin_dir:-$bin_root}
config_dir=${config_dir:-$config_root/goalrouter}
state_dir=${state_dir:-$state_root/goalrouter}
codex_home=${codex_home:-${CODEX_HOME:-"$HOME/.codex"}}
control_dir=$bin_dir/.goalrouter-control

for required_tool in awk cat chmod cp curl docker find grep id mkdir mktemp mv od rm sed sort tar tr uname; do
    require_tool "$required_tool"
done
install_uid=$(id -u) || fail 'cannot determine invoking user ID'
install_gid=$(id -g) || fail 'cannot determine invoking group ID'
if command -v sha256sum >/dev/null 2>&1; then
    hash_command=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    hash_command='shasum -a 256'
else
    fail 'required checksum tool is unavailable: sha256sum or shasum'
fi

check_destination "$bin_dir" 'bin directory'
check_destination "$config_dir" 'config directory'
check_destination "$state_dir" 'state directory'
check_destination "$control_dir" 'control directory'
[ ! -e "$control_dir/.uninstalling" ] && [ ! -L "$control_dir/.uninstalling" ] \
    || fail 'an uninstall recovery is active; resume uninstall before installing'
valid_path "$codex_home" || fail 'invalid Codex home path'
[ "$bin_dir" != "$bin_root" ] || :
[ "$config_dir" != "$config_root" ] || fail 'config directory cannot be the XDG config root'
[ "$state_dir" != "$state_root" ] || fail 'state directory cannot be the XDG state root'
paths_overlap "$bin_dir" "$config_dir" && fail 'bin and config directories overlap'
paths_overlap "$bin_dir" "$state_dir" && fail 'bin and state directories overlap'
paths_overlap "$config_dir" "$state_dir" && fail 'config and state directories overlap'

existing_metadata=0
directory_sentinel=.goalrouter-owned-v1
for existing_name in install.json ownership.sha256 image-ref image-digest launcher-version owned-installer owned-uninstaller; do
    if [ -e "$state_dir/$existing_name" ] || [ -L "$state_dir/$existing_name" ]; then
        existing_metadata=1
    fi
done
if [ "$existing_metadata" -eq 1 ]; then
    existing_valid=1
    [ -f "$state_dir/install.json" ] && [ ! -L "$state_dir/install.json" ] \
        && [ -f "$state_dir/ownership.sha256" ] && [ ! -L "$state_dir/ownership.sha256" ] \
        || existing_valid=0
    if [ "$existing_valid" -eq 1 ]; then
        if [ "$hash_command" = sha256sum ]; then
            (CDPATH='' cd "$state_dir" && sha256sum -c ownership.sha256) >/dev/null 2>&1 \
                || existing_valid=0
        else
            (CDPATH='' cd "$state_dir" && shasum -a 256 -c ownership.sha256) >/dev/null 2>&1 \
                || existing_valid=0
        fi
        grep -Eq '^\{"manifest_version":1,"protocol_version":1,' "$state_dir/install.json" \
            || existing_valid=0
    fi
    if [ "$existing_valid" -eq 0 ] && [ "$force_repair" -eq 0 ]; then
        fail 'existing installation metadata is corrupt; use --force only after verifying destinations'
    fi
fi
if [ -d "$config_dir" ] && [ "$existing_metadata" -eq 0 ]; then
    unexpected_config=$(find "$config_dir" -mindepth 1 -maxdepth 1 \
        ! -name task-models.yaml ! -name "$directory_sentinel" -print -quit) \
        || fail 'cannot inspect pre-existing config directory'
    [ -z "$unexpected_config" ] \
        || fail 'pre-existing config directory contains unowned entries'
    if [ -e "$config_dir/task-models.yaml" ] || [ -L "$config_dir/task-models.yaml" ]; then
        [ -f "$config_dir/task-models.yaml" ] && [ ! -L "$config_dir/task-models.yaml" ] \
            || fail 'pre-existing configuration is not a regular file'
    fi
fi
if [ -d "$state_dir" ] && [ "$existing_metadata" -eq 0 ]; then
    existing_state_entry=$(find "$state_dir" -mindepth 1 -maxdepth 1 -print -quit) \
        || fail 'cannot inspect pre-existing state directory'
    if [ -n "$existing_state_entry" ]; then
        [ -f "$state_dir/$directory_sentinel" ] && [ ! -L "$state_dir/$directory_sentinel" ] \
            || fail 'pre-existing nonempty state directory is not GoalRouter-owned'
        [ "$(cat "$state_dir/$directory_sentinel")" = goalrouter-owned-directory-v1 ] \
            || fail 'pre-existing state ownership sentinel is invalid'
    fi
fi

case $(uname -m) in
    x86_64) host_arch=amd64 ;;
    aarch64 | arm64) host_arch=arm64 ;;
    *) fail 'unsupported host architecture' ;;
esac
platform=linux/$host_arch
docker version >/dev/null 2>&1 || fail 'Docker CLI or daemon is unavailable'

if [ "$version" = latest ]; then
    [ "$release_base_explicit" -eq 0 ] \
        || fail '--version latest is unsupported with a custom release base'
    latest_url=$(curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --silent --show-error \
        --head --output /dev/null --write-out '%{url_effective}' \
        'https://github.com/vparla/GoalRouter/releases/latest') \
        || fail 'cannot resolve latest stable release'
    version=${latest_url##*/v}
fi
valid_version "$version" || fail 'version must be stable semantic version x.y.z'

if [ -z "$release_base" ]; then
    release_base=https://github.com/vparla/GoalRouter/releases/download/v$version
fi
case $release_base in
    https://*)
        valid_https_release_base "$release_base" \
            || fail 'invalid HTTPS release base'
        transport=https
        ;;
    http://*)
        loopback_authority=${release_base#http://}
        loopback_authority=${loopback_authority%%/*}
        case $loopback_authority in
            127.0.0.1 | localhost | '[::1]') ;;
            127.0.0.1:* | localhost:*)
                loopback_port=${loopback_authority#*:}
                case $loopback_port in '' | *[!0-9]*) fail 'loopback HTTP port is invalid' ;; esac
                ;;
            '[::1]:'*)
                loopback_port=${loopback_authority#'[::1]:'}
                case $loopback_port in '' | *[!0-9]*) fail 'loopback HTTP port is invalid' ;; esac
                ;;
            *) fail 'HTTP release sources must use a literal loopback authority' ;;
        esac
        [ "$allow_loopback_http" -eq 1 ] || fail 'loopback HTTP requires --allow-loopback-http'
        transport=loopback-http
        ;;
    *) fail 'release base must be HTTPS' ;;
esac
case $release_base in
    */) release_base=${release_base%/} ;;
esac

if [ -z "$image" ]; then
    image=$image_repository_option:$version
fi
valid_image "$image" || fail 'image must be an explicit registry/repository:tag reference'

if [ "$confirmed" -eq 0 ]; then
    [ -t 0 ] || fail 'confirmation required; use --yes for non-interactive installation'
    printf 'Install GoalRouter %s? [y/N] ' "$version" >&2
    IFS= read -r answer || fail 'confirmation failed'
    [ "$answer" = y ] || [ "$answer" = Y ] || fail 'installation cancelled'
fi

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/goalrouter-install.XXXXXXXX") \
    || fail 'cannot create installation staging directory'
rollback_file=$work_dir/rollback-paths
: >"$rollback_file"
rollback_temp_file=$work_dir/rollback-temps
: >"$rollback_temp_file"
archive_name=goalrouter-$version-unix.tar.gz
manifest_name=release-manifest.json
checksums=$work_dir/SHA256SUMS
archive=$work_dir/$archive_name
release_manifest=$work_dir/$manifest_name
download "$release_base/SHA256SUMS" "$checksums"
download "$release_base/$manifest_name" "$release_manifest"
download "$release_base/$archive_name" "$archive"

manifest_expected_digest=$(awk -v wanted="$manifest_name" '
    $NF == wanted || $NF == "*" wanted {
        count += 1
        if (NF != 2 || $1 !~ /^[0-9A-Fa-f]{64}$/) bad = 1
        digest = tolower($1)
    }
    END { if (count != 1 || bad) exit 1; print digest }
' "$checksums") || fail 'checksum manifest must contain exactly one valid release manifest entry'
if [ "$hash_command" = sha256sum ]; then
    manifest_actual_digest=$(sha256sum "$release_manifest" | awk '{print $1}') \
        || fail 'cannot hash downloaded release manifest'
else
    manifest_actual_digest=$(shasum -a 256 "$release_manifest" | awk '{print $1}') \
        || fail 'cannot hash downloaded release manifest'
fi
[ "$manifest_actual_digest" = "$manifest_expected_digest" ] \
    || fail 'downloaded release manifest checksum mismatch'

awk 'NR > 1 { bad = 1 } END { exit (bad || NR != 1) }' "$release_manifest" \
    || fail 'release manifest must contain exactly one canonical record'
manifest_line=$(cat "$release_manifest") || fail 'cannot read release manifest'
valid_text "$manifest_line" || fail 'release manifest contains invalid bytes'
manifest_version=$(printf '%s\n' "$manifest_line" | sed -n 's/^{"version":"\([^"]*\)".*/\1/p')
manifest_protocol=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"protocol_version":\([0-9][0-9]*\),.*/\1/p')
manifest_image=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"image":"\([^"]*\)","image_digest".*/\1/p')
manifest_image_digest=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"image_digest":"\([^"]*\)","architectures".*/\1/p')
manifest_architectures=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"architectures":\[\([^]]*\)\],"source_revision".*/\1/p')
manifest_revision=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"source_revision":"\([^"]*\)","minimum_hosts".*/\1/p')
manifest_minimum_windows=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"minimum_hosts":{"windows":"\([^"]*\)",.*/\1/p')
manifest_minimum_powershell=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"powershell":"\([^"]*\)",.*/\1/p')
manifest_minimum_wsl=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"wsl":"\([^"]*\)",.*/\1/p')
manifest_minimum_docker=$(printf '%s\n' "$manifest_line" | sed -n 's/^.*"docker":"\([^"]*\)"}}$/\1/p')
canonical_manifest=$(printf '{"version":"%s","protocol_version":%s,"image":"%s","image_digest":"%s","architectures":[%s],"source_revision":"%s","minimum_hosts":{"windows":"%s","powershell":"%s","wsl":"%s","docker":"%s"}}' \
    "$manifest_version" "$manifest_protocol" "$manifest_image" "$manifest_image_digest" \
    "$manifest_architectures" "$manifest_revision" "$manifest_minimum_windows" \
    "$manifest_minimum_powershell" "$manifest_minimum_wsl" "$manifest_minimum_docker")
[ "$manifest_line" = "$canonical_manifest" ] || fail 'release manifest schema is invalid'
[ "$manifest_version" = "$version" ] || fail 'release manifest version does not match request'
[ "$manifest_protocol" = 1 ] || fail 'release manifest launcher protocol major is not 1'
[ "$manifest_image" = "$image" ] || fail 'release manifest image does not match request'
valid_digest "$manifest_image_digest" || fail 'release manifest image digest is invalid'
valid_text "$manifest_revision" || fail 'release manifest source revision is invalid'
for manifest_minimum in "$manifest_minimum_windows" "$manifest_minimum_powershell" \
    "$manifest_minimum_wsl"
do
    printf '%s\n' "$manifest_minimum" | grep -Eq '^[0-9]+([.][0-9]+){1,3}$' \
        || fail 'release manifest minimum host version is invalid'
done
printf '%s\n' "$manifest_minimum_docker" | grep -Eq '^[0-9]+([.][0-9]+){1,3}$' \
    || fail 'release manifest minimum Docker version is invalid'
case $manifest_architectures in
    '"linux/amd64"' | '"linux/arm64"' | '"linux/amd64","linux/arm64"' | '"linux/arm64","linux/amd64"') ;;
    *) fail 'release manifest architectures are invalid' ;;
esac
docker_host_versions=$(docker version --format '{{.Client.Version}} {{.Server.Version}}') \
    || fail 'cannot inspect Docker client and daemon versions'
# Word splitting is intentional over Docker's fixed two-field format.
# shellcheck disable=SC2086
set -- $docker_host_versions
[ "$#" -eq 2 ] || fail 'Docker version output is invalid'
version_at_least "$1" "$manifest_minimum_docker" \
    || fail 'Docker client version is below the release minimum or invalid'
version_at_least "$2" "$manifest_minimum_docker" \
    || fail 'Docker daemon version is below the release minimum or invalid'
manifest_platform_token=$(printf '"%s"' "$platform")
case $manifest_architectures in
    *"$manifest_platform_token"*) ;;
    *) fail 'release manifest does not support the host platform' ;;
esac

expected_digest=$(awk -v wanted="$archive_name" '
    $NF == wanted || $NF == "*" wanted {
        count += 1
        if (NF != 2 || $1 !~ /^[0-9A-Fa-f]{64}$/) bad = 1
        digest = tolower($1)
    }
    END { if (count != 1 || bad) exit 1; print digest }
' "$checksums") || fail 'checksum manifest must contain exactly one valid asset entry'
if [ "$hash_command" = sha256sum ]; then
    actual_digest=$(sha256sum "$archive" | awk '{print $1}') \
        || fail 'cannot hash downloaded archive'
else
    actual_digest=$(shasum -a 256 "$archive" | awk '{print $1}') \
        || fail 'cannot hash downloaded archive'
fi
[ "$actual_digest" = "$expected_digest" ] || fail 'downloaded archive checksum mismatch'

member_list=$work_dir/archive-members
tar -tzf "$archive" >"$member_list" 2>"$work_dir/archive-list-errors" \
    || fail 'cannot list downloaded archive'
[ ! -s "$work_dir/archive-list-errors" ] \
    || fail 'archive required unsafe name normalization'
member_count=0
goalrouter_count=0
installer_count=0
uninstaller_count=0
while IFS= read -r member; do
    member_count=$((member_count + 1))
    case $member in
        goalrouter) goalrouter_count=$((goalrouter_count + 1)) ;;
        install.sh) installer_count=$((installer_count + 1)) ;;
        uninstall.sh) uninstaller_count=$((uninstaller_count + 1)) ;;
        *) fail "archive contains an unexpected or unsafe member: $member" ;;
    esac
done <"$member_list"
[ "$member_count" -eq 3 ] && [ "$goalrouter_count" -eq 1 ] \
    && [ "$installer_count" -eq 1 ] && [ "$uninstaller_count" -eq 1 ] \
    || fail 'archive members are missing or duplicated'
tar -tvzf "$archive" >"$work_dir/archive-details" 2>/dev/null \
    || fail 'cannot inspect downloaded archive'
while IFS= read -r detail; do
    mode=${detail%% *}
    [ "$mode" = '-rwxr-xr-x' ] || fail 'archive member has an unsafe type or mode'
done <"$work_dir/archive-details"
mkdir "$work_dir/extract"
tar -xzf "$archive" -C "$work_dir/extract" goalrouter install.sh uninstall.sh \
    || fail 'cannot extract verified archive'

docker pull "$image" >/dev/null || fail 'cannot pull requested image'
image_arch=$(docker image inspect --format '{{.Architecture}}' "$image") \
    || fail 'cannot inspect requested image architecture'
[ "$image_arch" = "$host_arch" ] || fail 'requested image architecture does not match host architecture'
repo_digest_lines=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image") \
    || fail 'cannot resolve requested image digest'
repo_digest=
repo_digest_count=0
while IFS= read -r candidate_digest; do
    [ -n "$candidate_digest" ] || continue
    case $candidate_digest in
        "$image_repository"@sha256:*)
            repo_digest=$candidate_digest
            repo_digest_count=$((repo_digest_count + 1))
            ;;
    esac
done <<EOF
$repo_digest_lines
EOF
[ "$repo_digest_count" -eq 1 ] || fail 'image must resolve to one canonical repository digest'
image_digest=${repo_digest#*@}
valid_digest "$image_digest" || fail 'resolved image digest is invalid'
[ "$image_digest" = "$manifest_image_digest" ] \
    || fail 'pulled image digest does not match trusted release manifest'

source_revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image") \
    || fail 'cannot inspect candidate source revision'
valid_text "$source_revision" || fail 'candidate source revision is invalid'
[ "$source_revision" = "$manifest_revision" ] \
    || fail 'candidate source revision does not match trusted release manifest'

runtime_json=$(docker run --rm --read-only --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    "$repo_digest" --json version) || fail 'candidate image version query failed'
runtime_protocol=$(printf '%s\n' "$runtime_json" \
    | sed -n 's/.*"protocol_version":[[:space:]]*\([0-9][0-9]*\).*/\1/p')
runtime_version=$(printf '%s\n' "$runtime_json" \
    | sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p')
[ "$runtime_protocol" = 1 ] || fail 'candidate launcher protocol major is not 1'
[ "$runtime_version" = "$version" ] || fail 'candidate application version does not match requested version'

candidate_config=$work_dir/task-models.yaml
docker run --rm --read-only --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    "$repo_digest" config template >"$candidate_config" \
    || fail 'cannot emit candidate configuration template'
[ -s "$candidate_config" ] || fail 'candidate configuration template is empty'
docker run --rm --read-only --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --user "$install_uid:$install_gid" \
    --mount "type=bind,src=$candidate_config,dst=/candidate.yaml,readonly" \
    --env GOALROUTER_CONFIG=/candidate.yaml \
    "$repo_digest" config validate >/dev/null \
    || fail 'candidate configuration template is invalid'
config_file=$config_dir/task-models.yaml
if [ -e "$config_file" ] && [ "$reset_config" -eq 0 ]; then
    [ -f "$config_file" ] && [ ! -L "$config_file" ] \
        || fail 'existing configuration is not a regular file'
    docker run --rm --read-only --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
        --user "$install_uid:$install_gid" \
        --mount "type=bind,src=$config_file,dst=/candidate.yaml,readonly" \
        --env GOALROUTER_CONFIG=/candidate.yaml \
        "$repo_digest" config validate >/dev/null \
        || fail 'existing configuration is incompatible with candidate image'
fi

metadata_stage=$work_dir/metadata
mkdir "$metadata_stage"
write_value image-ref "$image_repository"
write_value image-digest "$image_digest"
write_value launcher-version "$version"
write_value protocol-version 1
write_value app-version "$runtime_version"
write_value image-platform "$platform"
write_value source-revision "$source_revision"
write_value release-base "$release_base"
write_value release-transport "$transport"
write_value image-repository "$image_repository"
write_value owned-home "$HOME"
write_value owned-bin-dir "$bin_dir"
write_value owned-config-dir "$config_dir"
write_value owned-state-dir "$state_dir"
write_value owned-codex-home "$codex_home"
write_value owned-launcher "$bin_dir/goalrouter"
write_value owned-installer "$bin_dir/goalrouter-install"
write_value owned-uninstaller "$bin_dir/goalrouter-uninstall"
write_value guard-config-root "$config_root"
write_value guard-state-root "$state_root"
config_parent=${config_dir%/*}; [ -n "$config_parent" ] || config_parent=/
state_parent=${state_dir%/*}; [ -n "$state_parent" ] || state_parent=/
bin_parent=${bin_dir%/*}; [ -n "$bin_parent" ] || bin_parent=/
write_value guard-config-parent "$config_parent"
write_value guard-state-parent "$state_parent"
write_value guard-bin-parent "$bin_parent"

cat >"$metadata_stage/install.json" <<EOF
{"manifest_version":1,"protocol_version":1,"version":"$(json_escape "$version")","launcher_version":"$(json_escape "$version")","image_reference":"$(json_escape "$image_repository")","image_digest":"$(json_escape "$image_digest")","image_platform":"$(json_escape "$platform")","source_revision":"$(json_escape "$source_revision")","owned":{"launcher":"$(json_escape "$bin_dir/goalrouter")","installer":"$(json_escape "$bin_dir/goalrouter-install")","uninstaller":"$(json_escape "$bin_dir/goalrouter-uninstall")","config_dir":"$(json_escape "$config_dir")","state_dir":"$(json_escape "$state_dir")"}}
EOF
ownership_names='install.json image-ref image-digest launcher-version protocol-version app-version image-platform source-revision release-base release-transport image-repository owned-home owned-bin-dir owned-config-dir owned-state-dir owned-codex-home owned-launcher owned-installer owned-uninstaller guard-config-root guard-state-root guard-config-parent guard-state-parent guard-bin-parent'
if [ "$hash_command" = sha256sum ]; then
    # Word splitting is intentional over a fixed internal filename list.
    # shellcheck disable=SC2086
    (CDPATH='' cd "$metadata_stage" && sha256sum $ownership_names) >"$metadata_stage/ownership.sha256"
else
    # Word splitting is intentional over a fixed internal filename list.
    # shellcheck disable=SC2086
    (CDPATH='' cd "$metadata_stage" && shasum -a 256 $ownership_names) >"$metadata_stage/ownership.sha256"
fi

transaction_active=1
mkdir -p "$bin_dir" "$config_dir" "$state_dir" "$control_dir"
for owned_directory in "$bin_dir" "$config_dir" "$state_dir" "$control_dir"; do
    [ -d "$owned_directory" ] && [ ! -L "$owned_directory" ] \
        || fail 'owned installation directory is missing or unsafe'
    [ "$(find "$owned_directory" -prune -user "$install_uid" -print)" = "$owned_directory" ] \
        || fail 'owned installation directory has an unexpected owner'
    [ -z "$(find "$owned_directory" -prune -perm -0022 -print)" ] \
        || fail 'owned installation directory cannot be group/world writable'
done

replace_owned() {
    replace_source=$1
    replace_target=$2
    replace_mode=$3
    replace_temp=$(mktemp "$replace_target.goalrouter-tmp.XXXXXXXX") \
        || fail "cannot create exclusive replacement: $replace_target"
    printf '%s\n' "$replace_temp" >>"$rollback_temp_file"
    replace_backup=$replace_target.goalrouter-backup.$$
    [ -f "$replace_temp" ] && [ ! -L "$replace_temp" ] \
        || fail "unsafe temporary replacement: $replace_target"
    [ ! -e "$replace_backup" ] && [ ! -L "$replace_backup" ] \
        || fail "backup replacement collision: $replace_target"
    [ ! -L "$replace_target" ] || fail "owned target became a symbolic link: $replace_target"
    cp "$replace_source" "$replace_temp" || fail "cannot stage replacement: $replace_target"
    chmod "$replace_mode" "$replace_temp" || fail "cannot set replacement mode: $replace_target"
    if [ -e "$replace_target" ]; then
        [ -f "$replace_target" ] || fail "owned target is not a regular file: $replace_target"
        printf 'existing %s\n' "$replace_target" >>"$rollback_file"
        mv "$replace_target" "$replace_backup" || fail "cannot back up owned target: $replace_target"
    else
        printf 'new %s\n' "$replace_target" >>"$rollback_file"
    fi
    mv "$replace_temp" "$replace_target" || fail "cannot activate replacement: $replace_target"
}

printf '%s' goalrouter-owned-directory-v1 >"$work_dir/directory-sentinel"
replace_owned "$work_dir/directory-sentinel" "$config_dir/$directory_sentinel" 0600
replace_owned "$work_dir/directory-sentinel" "$state_dir/$directory_sentinel" 0600
replace_owned "$work_dir/extract/goalrouter" "$bin_dir/goalrouter" 0755
replace_owned "$work_dir/extract/install.sh" "$bin_dir/goalrouter-install" 0755
replace_owned "$work_dir/extract/uninstall.sh" "$bin_dir/goalrouter-uninstall" 0755
if [ ! -e "$config_file" ] || [ "$reset_config" -eq 1 ]; then
    replace_owned "$candidate_config" "$config_file" 0600
fi
for metadata_name in image-ref image-digest launcher-version protocol-version app-version image-platform source-revision release-base release-transport image-repository owned-home owned-bin-dir owned-config-dir owned-state-dir owned-codex-home owned-launcher owned-installer owned-uninstaller guard-config-root guard-state-root guard-config-parent guard-state-parent guard-bin-parent ownership.sha256 install.json; do
    replace_owned "$metadata_stage/$metadata_name" "$control_dir/$metadata_name" 0600
    replace_owned "$metadata_stage/$metadata_name" "$state_dir/$metadata_name" 0600
done

if [ "$skip_doctor" -eq 0 ]; then
    "$bin_dir/goalrouter" --config "$config_file" --state-dir "$state_dir" \
        --codex-home "$codex_home" doctor \
        || fail 'installed doctor failed; installation rolled back'
fi

while IFS=' ' read -r replaced_kind replaced_target; do
    [ -n "$replaced_kind" ] && [ -n "$replaced_target" ] || continue
    rm -f "$replaced_target.goalrouter-backup.$$"
done <"$rollback_file"
transaction_active=0

case :$PATH: in
    *:"$bin_dir":*) ;;
    *)
        if [ "$path_hint" -eq 1 ]; then
            printf 'GoalRouter installed, but %s is not on PATH.\n' "$bin_dir" >&2
            printf 'Add this exact directory to PATH in your shell configuration.\n' >&2
        fi
        ;;
esac
printf 'GoalRouter %s installed at %s\n' "$version" "$bin_dir/goalrouter"
