#!/bin/sh
set -eu
umask 077

case $0 in
    */*) script_directory=${0%/*} ;;
    *) script_directory=. ;;
esac
script_directory=$(CDPATH='' cd -P "$script_directory" && pwd -P)
# shellcheck source=scripts/docker-resource-cleanup.sh
. "$script_directory/docker-resource-cleanup.sh"

fail() {
    printf 'runtime image smoke: %s\n' "$*" >&2
    exit 1
}

assert_contains() {
    value=$1
    expected=$2
    case $value in
        *"$expected"*) ;;
        *) fail "expected output containing: $expected" ;;
    esac
}

owner_label='org.goalrouter.test=runtime-image-smoke'
smoke_run_id=$(cat /etc/hostname)
run_label="org.goalrouter.test.run=$smoke_run_id"
image="goalrouter-runtime-smoke:${smoke_run_id}"
project_volume="goalrouter-runtime-smoke-project-${smoke_run_id}"
config_volume="goalrouter-runtime-smoke-config-${smoke_run_id}"
auth_volume="goalrouter-runtime-smoke-auth-${smoke_run_id}"
state_volume="goalrouter-runtime-smoke-state-${smoke_run_id}"
post_signal_volume="goalrouter-runtime-smoke-post-signal-${smoke_run_id}"
volumes="$project_volume $config_volume $auth_volume $state_volume"
gr_cleanup_init runtime-image-smoke "$smoke_run_id" \
    'runtime image smoke cleanup'
gr_install_cleanup_traps
mkdir -p "${HOME:?}"

docker build \
    --quiet \
    --pull=false \
    --target runtime \
    --build-arg VERSION=1.0.8 \
    --build-arg REVISION=runtime-smoke \
    --build-arg CREATED=1970-01-01T00:00:00Z \
    --label "$owner_label" \
    --label "$run_label" \
    --tag "$image" \
    /workspace >/tmp/runtime-image-id
runtime_image_id=$(docker image inspect --format '{{.Id}}' "$image")
[ -n "$runtime_image_id" ] || fail "built image ID is empty for $image"
gr_cleanup_register_image "$image" "$runtime_image_id"
gr_verify_image "$image" || fail 'built image ownership does not match current smoke run'

for volume in $volumes; do
    docker volume create \
        --label "$owner_label" \
        --label "$run_label" \
        "$volume" >/dev/null
    gr_verify_volume "$volume" \
        || fail "volume ownership does not match current smoke run: $volume"
done

if [ "${RUNTIME_SMOKE_HOLD_AFTER_FIXTURES:-0}" -eq 1 ]; then
    sync_label=${RUNTIME_SMOKE_SYNC_LABEL:?}
    hold_name="goalrouter-runtime-smoke-hold-${smoke_run_id}"
    docker container create \
        --name "$hold_name" \
        --label "$owner_label" \
        --label "$run_label" \
        --label "$sync_label" \
        --entrypoint /bin/sh \
        "$image" -c 'exec sleep 3600' >/dev/null
    docker container start "$hold_name" >/dev/null
    hold_pipe=/tmp/runtime-image-smoke-hold
    mkfifo "$hold_pipe"
    set +e
    IFS= read -r _ < "$hold_pipe"
    set -e
    docker volume create \
        --label "$owner_label" \
        --label "$run_label" \
        "$post_signal_volume" >/dev/null
    fail "test hold unexpectedly released; post-signal work continued"
fi

docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --user 0:0 \
    --entrypoint /bin/sh \
    --volume "$project_volume:/project:rw" \
    --volume "$config_volume:/config:rw" \
    --volume "$auth_volume:/codex-auth:rw" \
    --volume "$state_volume:/state:rw" \
    "$image" -eu -c '
        printf "project-immutable\n" > /project/immutable.txt
        printf "schema-version: 1\n" > /config/task-models.yaml
        printf "{}\n" > /codex-auth/auth.json
        chmod 0555 /project /config /codex-auth
        chmod 0444 /project/immutable.txt /config/task-models.yaml /codex-auth/auth.json
        chown 24680:24681 /state
        chmod 0700 /state
    '

entrypoint=$(docker image inspect --format '{{json .Config.Entrypoint}}' "$image")
[ "$entrypoint" = '["/usr/local/bin/goalrouter-container-entrypoint"]' ] \
    || fail "runtime entrypoint is not the executable container entrypoint"
default_user=$(docker image inspect --format '{{.Config.User}}' "$image")
[ "$default_user" = '10001:10001' ] || fail "runtime default user is not 10001:10001"
image_size=$(docker image inspect --format '{{.Size}}' "$image")

docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --entrypoint /bin/sh \
    "$image" -eu -c '
        test -x /bin/sh
        test -r /etc/ssl/certs/ca-certificates.crt
        command -v git >/dev/null
        for tool in pip pip3 pytest ruff mypy gcc cc make docker; do
            if command -v "$tool" >/dev/null 2>&1; then
                exit 26
            fi
        done
        test ! -e /workspace/src
        test ! -e /workspace/tests
        test ! -e /workspace/pyproject.toml
        test ! -e /wheels
        test ! -e /root/.cache/pip
        python -c "import goalrouter"
    '

immutable_before=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --entrypoint /usr/bin/sha256sum \
    --volume "$project_volume:/project:ro" \
    --volume "$config_volume:/config:ro" \
    --volume "$auth_volume:/codex-auth:ro" \
    "$image" \
    /project/immutable.txt /config/task-models.yaml /codex-auth/auth.json)

version_output=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --env GOALROUTER_CONFIG=/config/task-models.yaml \
    --env GOALROUTER_STATE_PATH=/state \
    --volume "$project_volume:/project:ro" \
    --volume "$config_volume:/config:ro" \
    --volume "$auth_volume:/codex-auth:ro" \
    --volume "$state_volume:/state:rw" \
    "$image" --json version)
assert_contains "$version_output" '"version": "1.0.8"'
assert_contains "$version_output" '"image_revision": "runtime-smoke"'

template_output=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --env GOALROUTER_CONFIG=/config/task-models.yaml \
    --volume "$project_volume:/project:ro" \
    --volume "$config_volume:/config:ro" \
    --volume "$auth_volume:/codex-auth:ro" \
    "$image" config template)
assert_contains "$template_output" '# File: config/task-models.template.yaml'
assert_contains "$template_output" 'schema-version: 1'

docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --user 24680:24681 \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --env GOALROUTER_CONFIG=/config/task-models.yaml \
    --volume "$project_volume:/project:ro" \
    --volume "$config_volume:/config:ro" \
    --volume "$auth_volume:/codex-auth:ro" \
    --volume "$state_volume:/state:rw" \
    --entrypoint /bin/sh \
    "$image" -eu -c '
        if command -v getent >/dev/null 2>&1; then
            if getent passwd 24680 >/dev/null; then
                exit 21
            fi
        elif grep -q "^24680:" /etc/passwd; then
            exit 21
        fi
        test -x /usr/local/bin/goalrouter-container-entrypoint
        printf "#!/bin/sh\nexit 0\n" > /tmp/executable-probe
        chmod 0700 /tmp/executable-probe
        /tmp/executable-probe
        grep " /tmp " /proc/mounts | grep -q nosuid
        /usr/local/bin/goalrouter-container-entrypoint --json version >/tmp/version.json
        grep -Fq "\"version\": \"1.0.8\"" /tmp/version.json
    '

docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --user 0:0 \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --entrypoint /bin/sh \
    "$image" -eu -c '
        if touch /rootfs-write-probe 2>/dev/null; then
            exit 22
        fi
    '

docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --user 0:0 \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --volume "$project_volume:/project:ro" \
    --volume "$config_volume:/config:ro" \
    --volume "$auth_volume:/codex-auth:ro" \
    --entrypoint /bin/sh \
    "$image" -eu -c '
        if (printf mutation >>/project/immutable.txt) 2>/dev/null; then
            exit 23
        fi
        if (printf mutation >>/config/task-models.yaml) 2>/dev/null; then
            exit 24
        fi
        if (printf mutation >>/codex-auth/auth.json) 2>/dev/null; then
            exit 25
        fi
    '

state_metadata=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --user 24680:24681 \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --volume "$state_volume:/state:rw" \
    --entrypoint /bin/sh \
    "$image" -eu -c '
        umask 077
        printf persistent-state > /state/ownership-probe
        stat -c %u:%g:%a /state/ownership-probe
    ')
[ "$state_metadata" = '24680:24681:600' ] \
    || fail "persistent state ownership or mode is incorrect: $state_metadata"

docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --network none \
    --user 24680:24681 \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --volume "$state_volume:/state:rw" \
    --entrypoint /bin/sh \
    "$image" -eu -c '
        grep -Fxq persistent-state /state/ownership-probe
        test "$(stat -c %u:%g:%a /state/ownership-probe)" = 24680:24681:600
    '

immutable_after=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --entrypoint /usr/bin/sha256sum \
    --volume "$project_volume:/project:ro" \
    --volume "$config_volume:/config:ro" \
    --volume "$auth_volume:/codex-auth:ro" \
    "$image" \
    /project/immutable.txt /config/task-models.yaml /codex-auth/auth.json)
[ "$immutable_after" = "$immutable_before" ] \
    || fail "immutable project, configuration, or authentication content changed"

cleanup_status=0
gr_cleanup_owned_resources || cleanup_status=$?
[ "$cleanup_status" -eq 0 ] || fail "owned resource cleanup failed"

remaining_containers=$(docker container ls --all --quiet --filter "label=$run_label")
[ -z "$remaining_containers" ] || fail "owned containers remain after cleanup"
remaining_volumes=$(docker volume ls --quiet --filter "label=$run_label")
[ -z "$remaining_volumes" ] || fail "owned volumes remain after cleanup"
remaining_images=$(docker image ls --quiet --filter "label=$run_label")
[ -z "$remaining_images" ] || fail "owned images remain after cleanup"

printf 'runtime image smoke: entrypoint, identities, mounts, state, content, and cleanup passed; size_bytes=%s\n' \
    "$image_size"
