#!/bin/sh
set -eu
umask 077

fail() {
    printf 'runtime image smoke interruption: %s\n' "$*" >&2
    exit 1
}

interrupt_owner_label='org.goalrouter.test=runtime-image-smoke-interrupt'
interrupt_id=$(cat /etc/hostname)
interrupt_label="org.goalrouter.test.interrupt=$interrupt_id"
sync_label="org.goalrouter.test.sync=$interrupt_id"
driver_name="goalrouter-runtime-smoke-interrupt-${interrupt_id}"
driver_id=
event_pid=
cleanup_complete=0
cleanup_result=0

cleanup_error() {
    printf 'runtime image smoke interruption cleanup: %s\n' "$*" >&2
    cleanup_failed=1
}

verify_driver_ownership() {
    resource=$1
    resource_owner=$(docker container inspect \
        --format '{{index .Config.Labels "org.goalrouter.test"}}' "$resource") \
        || {
            cleanup_error "cannot inspect driver container $resource"
            return 1
        }
    resource_interrupt=$(docker container inspect \
        --format '{{index .Config.Labels "org.goalrouter.test.interrupt"}}' "$resource") \
        || {
            cleanup_error "cannot inspect driver run label $resource"
            return 1
        }
    if [ "$resource_owner" != runtime-image-smoke-interrupt ] \
        || [ "$resource_interrupt" != "$interrupt_id" ]; then
        cleanup_error "refusing mismatched driver container $resource"
        return 1
    fi
}

cleanup() {
    if [ "$cleanup_complete" -eq 1 ]; then
        return "$cleanup_result"
    fi
    cleanup_failed=0

    if [ -n "$event_pid" ]; then
        if kill -0 "$event_pid" 2>/dev/null; then
            if ! kill "$event_pid"; then
                cleanup_error "cannot terminate Docker event listener $event_pid"
            fi
        fi
        event_status=0
        wait "$event_pid" || event_status=$?
        case $event_status in
            0|143) ;;
            *) cleanup_error "Docker event listener exited with status $event_status" ;;
        esac
        event_pid=
    fi

    driver_ids=$(docker container ls --all --quiet --filter "label=$interrupt_label") \
        || {
            cleanup_error "cannot enumerate owned driver containers"
            driver_ids=
        }
    for resource in $driver_ids; do
        if verify_driver_ownership "$resource"; then
            if ! docker container rm --force "$resource" >/dev/null; then
                cleanup_error "cannot remove owned driver container $resource"
            fi
        fi
    done

    cleanup_result=$cleanup_failed
    cleanup_complete=1
    return "$cleanup_result"
}

handle_exit() {
    exit_status=$?
    trap - EXIT HUP INT TERM
    cleanup_status=0
    cleanup || cleanup_status=$?
    if [ "$exit_status" -eq 0 ] && [ "$cleanup_status" -ne 0 ]; then
        exit_status=$cleanup_status
    fi
    exit "$exit_status"
}

handle_signal() {
    signal_status=$1
    trap - EXIT HUP INT TERM
    if ! cleanup; then
        printf 'runtime image smoke interruption: cleanup failed while handling signal\n' >&2
    fi
    exit "$signal_status"
}

trap 'handle_exit' EXIT
trap 'handle_signal 129' HUP
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

self_id=$(cat /etc/hostname)
workspace_source=$(docker container inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}' \
    "$self_id")
[ -n "$workspace_source" ] || fail "cannot resolve the host workspace mount"

driver_id=$(docker container create \
    --name "$driver_name" \
    --label "$interrupt_owner_label" \
    --label "$interrupt_label" \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,size=256m,mode=1777 \
    --env DOCKER_BUILDKIT=1 \
    --env HOME=/tmp/docker-home \
    --env RUNTIME_SMOKE_HOLD_AFTER_FIXTURES=1 \
    --env "RUNTIME_SMOKE_SYNC_LABEL=$sync_label" \
    --volume "$workspace_source:/workspace:ro" \
    --volume /var/run/docker.sock:/var/run/docker.sock:rw \
    --entrypoint /bin/sh \
    docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 \
    /workspace/scripts/runtime-image-smoke.sh)

smoke_run_id=$(printf '%s' "$driver_id" | cut -c 1-12)
run_label="org.goalrouter.test.run=$smoke_run_id"
hold_name="goalrouter-runtime-smoke-hold-${smoke_run_id}"
post_signal_volume="goalrouter-runtime-smoke-post-signal-${smoke_run_id}"
event_pipe=/tmp/runtime-image-smoke-event
event_since=$(date +%s)
event_until=$((event_since + 180))
mkfifo "$event_pipe"

docker events \
    --since "$event_since" \
    --until "$event_until" \
    --filter type=container \
    --filter event=start \
    --filter "label=$sync_label" \
    --format '{{.Actor.Attributes.name}}' > "$event_pipe" &
event_pid=$!

docker container start "$driver_id" >/dev/null
started_name=
IFS= read -r started_name < "$event_pipe" \
    || fail "Docker event listener ended before the hold container started"
if ! kill "$event_pid"; then
    fail "cannot terminate Docker event listener $event_pid"
fi
event_status=0
wait "$event_pid" || event_status=$?
event_pid=
[ "$event_status" -eq 143 ] \
    || fail "Docker event listener stopped with unexpected status $event_status"
[ "$started_name" = "$hold_name" ] \
    || fail "did not observe the owned hold container start"

docker kill --signal TERM "$driver_id" >/dev/null
driver_status=$(docker container wait "$driver_id")
docker container logs "$driver_id" >/tmp/runtime-image-smoke-driver.log 2>&1
[ "$driver_status" -eq 143 ] \
    || fail "expected signal status 143, got $driver_status"

if docker volume inspect "$post_signal_volume" >/dev/null 2>&1; then
    fail "post-signal work continued"
fi

remaining_containers=$(docker container ls --all --quiet --filter "label=$run_label")
[ -z "$remaining_containers" ] || fail "signal cleanup left owned containers"
remaining_volumes=$(docker volume ls --quiet --filter "label=$run_label")
[ -z "$remaining_volumes" ] || fail "signal cleanup left owned volumes"
remaining_images=$(docker image ls --quiet --filter "label=$run_label")
[ -z "$remaining_images" ] || fail "signal cleanup left owned images"

printf 'runtime image smoke interruption: TERM exited 143 without continuation or owned resources\n'
