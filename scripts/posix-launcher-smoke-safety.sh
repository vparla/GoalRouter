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
    printf 'posix launcher smoke safety: %s\n' "$*" >&2
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

fixture_owner_label='org.goalrouter.test=posix-launcher-smoke-safety'
safety_run_id=$(cat /etc/hostname)
fixture_run_label="org.goalrouter.test.run=$safety_run_id"
collision_volume="goalrouter-posix-launcher-smoke-$safety_run_id"
image=goalrouter-runtime:local
preexisting_image_id=
created_image_id=
gr_cleanup_init posix-launcher-smoke-safety "$safety_run_id" \
    'posix launcher smoke safety cleanup'
gr_install_cleanup_traps
mkdir -p "${HOME:?}"

if docker image inspect "$image" >/dev/null 2>&1; then
    preexisting_image_id=$(docker image inspect --format '{{.Id}}' "$image")
else
    docker build \
        --quiet \
        --pull=false \
        --target runtime \
        --build-arg VERSION=1.0.5 \
        --build-arg REVISION=posix-launcher-safety \
        --build-arg CREATED=1970-01-01T00:00:00Z \
        --label "$fixture_owner_label" \
        --label "$fixture_run_label" \
        --tag "$image" \
        /workspace >/tmp/posix-launcher-safety-image-id
    created_image_id=$(docker image inspect --format '{{.Id}}' "$image")
    preexisting_image_id=$created_image_id
    gr_cleanup_register_image "$image" "$created_image_id"
    gr_verify_image "$image" \
        || fail 'created pre-existing image fixture ownership is invalid'
fi

docker volume create \
    --label "$fixture_owner_label" \
    --label "$fixture_run_label" \
    "$collision_volume" >/dev/null
gr_verify_volume "$collision_volume" \
    || fail 'collision volume fixture ownership is invalid'

docker run --rm \
    --label "$fixture_owner_label" \
    --label "$fixture_run_label" \
    --volume "$collision_volume:/fixture:rw" \
    --entrypoint /bin/sh \
    docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 -eu -c \
    'printf "collision-sentinel\n" > /fixture/sentinel' >/dev/null

set +e
smoke_output=$(/bin/sh /workspace/scripts/posix-launcher-smoke.sh 2>&1)
smoke_status=$?
set -e
[ "$smoke_status" -ne 0 ] || fail 'expected ownership refusal but normal smoke passed'
assert_contains "$smoke_output" 'fixture volume ownership does not match current smoke run'

gr_verify_volume "$collision_volume" \
    || fail 'collision volume ownership changed during refusal'
sentinel=$(docker run --rm \
    --label "$fixture_owner_label" \
    --label "$fixture_run_label" \
    --volume "$collision_volume:/fixture:ro" \
    --entrypoint /bin/sh \
    docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 -eu -c 'cat /fixture/sentinel')
[ "$sentinel" = collision-sentinel ] \
    || fail 'collision-sentinel was not preserved'

current_preexisting_image_id=$(docker image inspect --format '{{.Id}}' "$image") \
    || fail 'pre-existing image tag was deleted'
[ "$current_preexisting_image_id" = "$preexisting_image_id" ] \
    || fail 'preserved image tag or ID changed'

cleanup_status=0
gr_cleanup_owned_resources || cleanup_status=$?
[ "$cleanup_status" -eq 0 ] || fail 'owned safety fixture cleanup failed'

printf 'posix launcher smoke safety: expected ownership refusal preserved collision-sentinel and pre-existing image tag/ID\n'
