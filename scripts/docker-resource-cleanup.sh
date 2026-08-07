#!/bin/sh

gr_cleanup_init() {
    GR_CLEANUP_OWNER=$1
    GR_CLEANUP_RUN=$2
    GR_CLEANUP_PREFIX=$3
    GR_CLEANUP_IMAGE_TAG=
    GR_CLEANUP_IMAGE_ID=
    GR_CLEANUP_COMPLETE=0
    GR_CLEANUP_RESULT=0
}

gr_cleanup_register_image() {
    GR_CLEANUP_IMAGE_TAG=$1
    GR_CLEANUP_IMAGE_ID=$2
}

gr_cleanup_error() {
    printf '%s: %s\n' "$GR_CLEANUP_PREFIX" "$*" >&2
    GR_CLEANUP_FAILED=1
}

gr_verify_container() {
    gr_resource=$1
    gr_owner=$(docker container inspect \
        --format '{{index .Config.Labels "org.goalrouter.test"}}' "$gr_resource") \
        || {
            gr_cleanup_error "cannot inspect container $gr_resource"
            return 1
        }
    gr_run=$(docker container inspect \
        --format '{{index .Config.Labels "org.goalrouter.test.run"}}' "$gr_resource") \
        || {
            gr_cleanup_error "cannot inspect container run label $gr_resource"
            return 1
        }
    if [ "$gr_owner" != "$GR_CLEANUP_OWNER" ] \
        || [ "$gr_run" != "$GR_CLEANUP_RUN" ]; then
        gr_cleanup_error "refusing mismatched container $gr_resource"
        return 1
    fi
}

gr_verify_volume() {
    gr_resource=$1
    gr_owner=$(docker volume inspect \
        --format '{{index .Labels "org.goalrouter.test"}}' "$gr_resource") \
        || {
            gr_cleanup_error "cannot inspect volume $gr_resource"
            return 1
        }
    gr_run=$(docker volume inspect \
        --format '{{index .Labels "org.goalrouter.test.run"}}' "$gr_resource") \
        || {
            gr_cleanup_error "cannot inspect volume run label $gr_resource"
            return 1
        }
    if [ "$gr_owner" != "$GR_CLEANUP_OWNER" ] \
        || [ "$gr_run" != "$GR_CLEANUP_RUN" ]; then
        gr_cleanup_error "refusing mismatched volume $gr_resource"
        return 1
    fi
}

gr_verify_image() {
    gr_resource=$1
    gr_owner=$(docker image inspect \
        --format '{{index .Config.Labels "org.goalrouter.test"}}' "$gr_resource") \
        || {
            gr_cleanup_error "cannot inspect image $gr_resource"
            return 1
        }
    gr_run=$(docker image inspect \
        --format '{{index .Config.Labels "org.goalrouter.test.run"}}' "$gr_resource") \
        || {
            gr_cleanup_error "cannot inspect image run label $gr_resource"
            return 1
        }
    if [ "$gr_owner" != "$GR_CLEANUP_OWNER" ] \
        || [ "$gr_run" != "$GR_CLEANUP_RUN" ]; then
        gr_cleanup_error "refusing mismatched image $gr_resource"
        return 1
    fi
}

gr_remove_container() {
    gr_resource=$1
    gr_verify_container "$gr_resource" || return 1
    docker container rm --force "$gr_resource" >/dev/null \
        || {
            gr_cleanup_error "cannot remove owned container $gr_resource"
            return 1
        }
}

gr_remove_volume() {
    gr_resource=$1
    gr_verify_volume "$gr_resource" || return 1
    docker volume rm --force "$gr_resource" >/dev/null \
        || {
            gr_cleanup_error "cannot remove owned volume $gr_resource"
            return 1
        }
}

gr_cleanup_owned_resources() {
    if [ "$GR_CLEANUP_COMPLETE" -eq 1 ]; then
        return "$GR_CLEANUP_RESULT"
    fi
    GR_CLEANUP_FAILED=0

    gr_containers=$(docker container ls --all --quiet \
        --filter "label=org.goalrouter.test.run=$GR_CLEANUP_RUN") \
        || {
            gr_cleanup_error 'cannot enumerate owned containers'
            gr_containers=
        }
    for gr_resource in $gr_containers; do
        gr_remove_container "$gr_resource" || :
    done

    gr_volumes=$(docker volume ls --quiet \
        --filter "label=org.goalrouter.test.run=$GR_CLEANUP_RUN") \
        || {
            gr_cleanup_error 'cannot enumerate owned volumes'
            gr_volumes=
        }
    for gr_resource in $gr_volumes; do
        gr_remove_volume "$gr_resource" || :
    done

    gr_images=$(docker image ls --quiet --no-trunc \
        --filter "label=org.goalrouter.test.run=$GR_CLEANUP_RUN") \
        || {
            gr_cleanup_error 'cannot enumerate owned images'
            gr_images=
        }
    for gr_resource in $gr_images; do
        if ! gr_verify_image "$gr_resource"; then
            continue
        fi
        if [ -z "$GR_CLEANUP_IMAGE_ID" ] \
            || [ "$gr_resource" != "$GR_CLEANUP_IMAGE_ID" ]; then
            gr_cleanup_error "refusing unregistered image $gr_resource"
            continue
        fi
        gr_current_id=$(docker image inspect --format '{{.Id}}' "$GR_CLEANUP_IMAGE_TAG") \
            || {
                gr_cleanup_error "cannot resolve owned image tag $GR_CLEANUP_IMAGE_TAG"
                continue
            }
        if [ "$gr_current_id" != "$GR_CLEANUP_IMAGE_ID" ]; then
            gr_cleanup_error "refusing changed image tag $GR_CLEANUP_IMAGE_TAG"
            continue
        fi
        if ! docker image rm "$GR_CLEANUP_IMAGE_TAG" >/dev/null; then
            gr_cleanup_error "cannot remove owned image tag $GR_CLEANUP_IMAGE_TAG"
        fi
    done

    GR_CLEANUP_RESULT=$GR_CLEANUP_FAILED
    GR_CLEANUP_COMPLETE=1
    return "$GR_CLEANUP_RESULT"
}

gr_handle_exit() {
    gr_exit_status=$?
    trap - EXIT HUP INT TERM
    gr_cleanup_status=0
    gr_cleanup_owned_resources || gr_cleanup_status=$?
    if [ "$gr_exit_status" -eq 0 ] && [ "$gr_cleanup_status" -ne 0 ]; then
        gr_exit_status=$gr_cleanup_status
    fi
    exit "$gr_exit_status"
}

gr_handle_signal() {
    gr_signal_status=$1
    trap - EXIT HUP INT TERM
    if ! gr_cleanup_owned_resources; then
        printf '%s: cleanup failed while handling signal\n' "$GR_CLEANUP_PREFIX" >&2
    fi
    exit "$gr_signal_status"
}

gr_install_cleanup_traps() {
    trap 'gr_handle_exit' EXIT
    trap 'gr_handle_signal 129' HUP
    trap 'gr_handle_signal 130' INT
    trap 'gr_handle_signal 143' TERM
}
