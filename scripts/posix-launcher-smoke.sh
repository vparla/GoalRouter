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
    printf 'posix launcher smoke: %s\n' "$*" >&2
    exit 1
}

assert_contains() {
    value=$1
    expected=$2
    case $value in
        *"$expected"*) ;;
        *) fail "expected JSON output containing: $expected" ;;
    esac
}

owner_label='org.goalrouter.test=posix-launcher-smoke'
smoke_run_id=$(cat /etc/hostname)
run_label="org.goalrouter.test.run=$smoke_run_id"
image=goalrouter-runtime:local
fixture_volume="goalrouter-posix-launcher-smoke-$smoke_run_id"
init_name="goalrouter-posix-launcher-init-$smoke_run_id"
built_image_id=
reused_image_id=
gr_cleanup_init posix-launcher-smoke "$smoke_run_id" \
    'posix launcher smoke cleanup'
gr_install_cleanup_traps
mkdir -p "${HOME:?}"

if docker image inspect "$image" >/dev/null 2>&1; then
    image_source=$(docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$image")
    image_license=$(docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.licenses"}}' "$image")
    image_version=$(docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$image")
    expected_revision=$(docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
    [ "$image_source" = https://github.com/vparla/GoalRouter ] \
        || fail "existing $image has an unexpected source label"
    [ "$image_license" = MIT ] \
        || fail "existing $image has an unexpected license label"
    [ "$image_version" = 1.0.4 ] \
        || fail "existing $image has an unexpected version label"
    [ -n "$expected_revision" ] \
        || fail "existing $image has no revision label"
    reused_image_id=$(docker image inspect --format '{{.Id}}' "$image")
    image_disposition=reused
else
    docker build \
        --quiet \
        --pull=false \
        --target runtime \
        --build-arg VERSION=1.0.4 \
        --build-arg REVISION=posix-launcher-smoke \
        --build-arg CREATED=1970-01-01T00:00:00Z \
        --label "$owner_label" \
        --label "$run_label" \
        --tag "$image" \
        /workspace >/tmp/posix-launcher-image-id
    built_image_id=$(docker image inspect --format '{{.Id}}' "$image")
    [ -n "$built_image_id" ] || fail "built image ID is empty for $image"
    gr_cleanup_register_image "$image" "$built_image_id"
    gr_verify_image "$image" \
        || fail "built image ownership does not match current smoke run"
    expected_revision=posix-launcher-smoke
    image_disposition=built
fi

docker volume create \
    --label "$owner_label" \
    --label "$run_label" \
    "$fixture_volume" >/dev/null
gr_verify_volume "$fixture_volume" \
    || fail 'fixture volume ownership does not match current smoke run'

docker container create \
    --name "$init_name" \
    --label "$owner_label" \
    --label "$run_label" \
    --volume "$fixture_volume:/fixture:rw" \
    docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 \
    /bin/sh -eu -c '
        mkdir -p /fixture/project /fixture/config /fixture/auth /fixture/state
        printf "immutable-target\n" > /fixture/project/immutable.txt
        printf "schema-version: 1\n" > /fixture/config/task-models.yaml
        printf "dummy-auth-must-not-be-printed\n" > /fixture/auth/auth.json
        chmod 0555 /fixture/project /fixture/config /fixture/auth
        chmod 0444 \
            /fixture/project/immutable.txt \
            /fixture/config/task-models.yaml \
            /fixture/auth/auth.json
        chmod 0555 /fixture/goalrouter
        chmod 0700 /fixture/state
    ' >/dev/null
docker cp /workspace/scripts/goalrouter "$init_name:/fixture/goalrouter"
docker container start --attach "$init_name" >/dev/null
gr_remove_container "$init_name"

immutable_before=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --volume "$fixture_volume:/fixture:ro" \
    --entrypoint sha256sum \
    docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 \
    /fixture/project/immutable.txt \
    /fixture/auth/auth.json)

fixture_mountpoint=$(docker volume inspect --format '{{.Mountpoint}}' "$fixture_volume")
[ -n "$fixture_mountpoint" ] || fail 'fixture volume mountpoint is empty'

version_output=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=64m,mode=1777 \
    --volume /var/run/docker.sock:/var/run/docker.sock:rw \
    --volume "$fixture_volume:$fixture_mountpoint:rw" \
    --env HOME=/tmp/launcher-home \
    docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 \
    "$fixture_mountpoint/goalrouter" \
    --project "$fixture_mountpoint/project" \
    --config "$fixture_mountpoint/config/task-models.yaml" \
    --state-dir "$fixture_mountpoint/state" \
    --codex-home "$fixture_mountpoint/auth" \
    --image "$image" \
    --json version)

assert_contains "$version_output" '"version": "1.0.4"'
assert_contains "$version_output" '"protocol_version": 1'
assert_contains "$version_output" "\"image_revision\": \"$expected_revision\""
case $version_output in
    *dummy-auth-must-not-be-printed*) fail 'dummy authentication content was printed' ;;
    *) ;;
esac

if [ -n "$reused_image_id" ]; then
    current_reused_image_id=$(docker image inspect --format '{{.Id}}' "$image") \
        || fail "cannot resolve reused image tag $image"
    [ "$current_reused_image_id" = "$reused_image_id" ] \
        || fail "reused image tag changed identity: $image"
fi

immutable_after=$(docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --volume "$fixture_volume:/fixture:ro" \
    --entrypoint sha256sum \
    docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 \
    /fixture/project/immutable.txt \
    /fixture/auth/auth.json)
[ "$immutable_after" = "$immutable_before" ] \
    || fail 'dummy target or authentication fixture changed'

cleanup_status=0
gr_cleanup_owned_resources || cleanup_status=$?
[ "$cleanup_status" -eq 0 ] || fail 'owned resource cleanup failed'

printf 'posix launcher smoke: JSON version/protocol, secret-negative output, hashes, and cleanup passed; image=%s\n' \
    "$image_disposition"
