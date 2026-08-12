#!/bin/sh
set -eu
umask 077

fail() {
    printf 'posix installer smoke: %s\n' "$*" >&2
    exit 1
}

live_inventory=${GOALROUTER_INSTALLED_LIVE_INVENTORY:-0}
case $live_inventory in 0 | 1) ;; *) fail 'invalid installed live inventory mode' ;; esac
if [ "$live_inventory" -eq 1 ]; then
    owner=posix-installed-live-inventory
    : "${GOALROUTER_CODEX_HOME_HOST:?}"
else
    owner=posix-installer-smoke
fi
run_id=$(cat /etc/hostname)
owner_label="org.goalrouter.test=$owner"
run_label="org.goalrouter.test.run=$run_id"
runtime_image="goalrouter-installer-smoke-runtime:$run_id"
registry_image='registry:3.0.0@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e'
registry_name="goalrouter-installer-registry-$run_id"
asset_name="goalrouter-installer-assets-$run_id"
subject_name="goalrouter-installer-subject-$run_id"
network_name="goalrouter-installer-network-$run_id"
release_volume="goalrouter-installer-release-$run_id"
home_volume="goalrouter-installer-home-$run_id"
runtime_image_id=
registry_reference=

verify_container() {
    resource=$1
    [ "$(docker container inspect --format '{{index .Config.Labels "org.goalrouter.test"}}' "$resource")" = "$owner" ] \
        && [ "$(docker container inspect --format '{{index .Config.Labels "org.goalrouter.test.run"}}' "$resource")" = "$run_id" ]
}

verify_volume() {
    resource=$1
    [ "$(docker volume inspect --format '{{index .Labels "org.goalrouter.test"}}' "$resource")" = "$owner" ] \
        && [ "$(docker volume inspect --format '{{index .Labels "org.goalrouter.test.run"}}' "$resource")" = "$run_id" ]
}

verify_network() {
    resource=$1
    [ "$(docker network inspect --format '{{index .Labels "org.goalrouter.test"}}' "$resource")" = "$owner" ] \
        && [ "$(docker network inspect --format '{{index .Labels "org.goalrouter.test.run"}}' "$resource")" = "$run_id" ]
}

cleanup() {
    cleanup_status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM
    for cleanup_container in "$subject_name" "$asset_name" "$registry_name"; do
        if docker container inspect "$cleanup_container" >/dev/null 2>&1; then
            if ! verify_container "$cleanup_container"; then
                printf 'posix installer smoke cleanup: refusing container %s\n' "$cleanup_container" >&2
                cleanup_failed=1
            elif ! docker container rm --force "$cleanup_container" >/dev/null; then
                printf 'posix installer smoke cleanup: cannot remove container %s\n' "$cleanup_container" >&2
                cleanup_failed=1
            fi
        fi
    done
    for cleanup_volume in "$release_volume" "$home_volume"; do
        if docker volume inspect "$cleanup_volume" >/dev/null 2>&1; then
            if ! verify_volume "$cleanup_volume"; then
                printf 'posix installer smoke cleanup: refusing volume %s\n' "$cleanup_volume" >&2
                cleanup_failed=1
            elif ! docker volume rm --force "$cleanup_volume" >/dev/null; then
                printf 'posix installer smoke cleanup: cannot remove volume %s\n' "$cleanup_volume" >&2
                cleanup_failed=1
            fi
        fi
    done
    if docker network inspect "$network_name" >/dev/null 2>&1; then
        if ! verify_network "$network_name"; then
            printf 'posix installer smoke cleanup: refusing network %s\n' "$network_name" >&2
            cleanup_failed=1
        elif ! docker network rm "$network_name" >/dev/null; then
            printf 'posix installer smoke cleanup: cannot remove network %s\n' "$network_name" >&2
            cleanup_failed=1
        fi
    fi
    if [ -n "$runtime_image_id" ]; then
        if [ -n "$registry_reference" ] && docker image inspect "$registry_reference" >/dev/null 2>&1; then
            current_registry_id=$(docker image inspect --format '{{.Id}}' "$registry_reference")
            if [ "$current_registry_id" != "$runtime_image_id" ]; then
                printf 'posix installer smoke cleanup: refusing changed registry tag\n' >&2
                cleanup_failed=1
            elif ! docker image rm "$registry_reference" >/dev/null; then
                printf 'posix installer smoke cleanup: cannot remove registry tag\n' >&2
                cleanup_failed=1
            fi
        fi
        if docker image inspect "$runtime_image" >/dev/null 2>&1; then
            current_runtime_id=$(docker image inspect --format '{{.Id}}' "$runtime_image")
            if [ "$current_runtime_id" != "$runtime_image_id" ]; then
                printf 'posix installer smoke cleanup: refusing changed runtime tag\n' >&2
                cleanup_failed=1
            elif ! docker image rm "$runtime_image" >/dev/null; then
                printf 'posix installer smoke cleanup: cannot remove runtime tag\n' >&2
                cleanup_failed=1
            fi
        fi
    fi
    if [ "$cleanup_status" -eq 0 ] && [ "$cleanup_failed" -ne 0 ]; then
        cleanup_status=1
    fi
    exit "$cleanup_status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker build \
    --quiet \
    --pull=false \
    --target runtime \
    --build-arg VERSION=1.0.5 \
    --build-arg REVISION=posix-installer-smoke \
    --build-arg CREATED=1970-01-01T00:00:00Z \
    --label "$owner_label" \
    --label "$run_label" \
    --tag "$runtime_image" \
    /workspace >/tmp/goalrouter-installer-runtime-id
runtime_image_id=$(docker image inspect --format '{{.Id}}' "$runtime_image")
[ -n "$runtime_image_id" ] || fail 'runtime image ID is empty'

docker network create --label "$owner_label" --label "$run_label" "$network_name" >/dev/null
verify_network "$network_name" || fail 'network ownership verification failed'
for volume in "$release_volume" "$home_volume"; do
    docker volume create --label "$owner_label" --label "$run_label" "$volume" >/dev/null
    verify_volume "$volume" || fail "volume ownership verification failed: $volume"
done
home_mountpoint=$(docker volume inspect --format '{{.Mountpoint}}' "$home_volume")
case $home_mountpoint in
    /*) ;;
    *) fail 'home volume mountpoint is not absolute' ;;
esac

docker container create \
    --name "$asset_name" \
    --label "$owner_label" \
    --label "$run_label" \
    --user 0:0 \
    --volume "$release_volume:/release:rw" \
    --volume "$home_volume:/smoke-home:rw" \
    --entrypoint /bin/sh \
    goalrouter-installer-smoke:local -eu -c '
        mkdir -p /tmp/release-asset
        cp /workspace/goalrouter /tmp/release-asset/goalrouter
        cp /workspace/install.sh /tmp/release-asset/install.sh
        cp /workspace/uninstall.sh /tmp/release-asset/uninstall.sh
        chmod 0755 /tmp/release-asset/goalrouter /tmp/release-asset/install.sh /tmp/release-asset/uninstall.sh
        tar -czf /release/goalrouter-1.0.5-unix.tar.gz -C /tmp/release-asset goalrouter install.sh uninstall.sh
        cd /release
        sha256sum goalrouter-1.0.5-unix.tar.gz >SHA256SUMS
        chown -R 24680:24681 /release /smoke-home
        chmod 0700 /smoke-home
    ' >/dev/null
verify_container "$asset_name" || fail 'asset initializer ownership verification failed'
docker container cp /workspace/scripts/goalrouter "$asset_name:/workspace/goalrouter"
docker container cp /workspace/scripts/install.sh "$asset_name:/workspace/install.sh"
docker container cp /workspace/scripts/uninstall.sh "$asset_name:/workspace/uninstall.sh"
docker container start --attach "$asset_name"
docker container rm "$asset_name" >/dev/null

docker container create \
    --name "$registry_name" \
    --label "$owner_label" \
    --label "$run_label" \
    --network "$network_name" \
    --publish 127.0.0.1::5000 \
    "$registry_image" >/dev/null
verify_container "$registry_name" || fail 'registry ownership verification failed'
docker container start "$registry_name" >/dev/null
docker container logs --follow "$registry_name" 2>&1 \
    | grep -m 1 -F 'listening on' >/dev/null \
    || fail 'ephemeral registry did not become ready'
registry_binding=$(docker port "$registry_name" 5000/tcp)
case $registry_binding in
    127.0.0.1:*) registry_port=${registry_binding##*:} ;;
    *) fail 'ephemeral registry did not bind exact loopback' ;;
esac
registry_reference=127.0.0.1:$registry_port/goalrouter:1.0.5
docker image tag "$runtime_image" "$registry_reference"
docker image push "$registry_reference" >/dev/null
registry_repo_digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$registry_reference" \
    | awk -v repository="${registry_reference%:*}" 'index($0, repository "@sha256:") == 1 { print substr($0, index($0, "@") + 1) }')
case $registry_repo_digest in sha256:????????????????????????????????????????????????????????????????) ;; *) fail 'registry RepoDigest is invalid' ;; esac
docker run --rm \
    --label "$owner_label" \
    --label "$run_label" \
    --user 0:0 \
    --volume "$release_volume:/release:rw" \
    --entrypoint /bin/sh \
    goalrouter-installer-smoke:local -eu -c '
        printf '\''{"version":"1.0.5","protocol_version":1,"image":"%s","image_digest":"%s","architectures":["linux/amd64","linux/arm64"],"source_revision":"posix-installer-smoke","minimum_hosts":{"windows":"10.0.19045","powershell":"5.1","wsl":"2.2.3","docker":"20.10"}}\n'\'' "$1" "$2" >/release/release-manifest.json
        cd /release
        sha256sum release-manifest.json goalrouter-1.0.5-unix.tar.gz >SHA256SUMS
        chown 24680:24681 release-manifest.json SHA256SUMS
    ' sh "$registry_reference" "$registry_repo_digest"

socket_gid=$(stat -c %g /var/run/docker.sock)
case $socket_gid in
    '' | *[!0-9]*) fail 'Docker socket group is not numeric' ;;
esac
if [ "$live_inventory" -eq 1 ]; then
    docker container create \
        --name "$subject_name" \
        --label "$owner_label" \
        --label "$run_label" \
        --network none \
        --user 24680:24681 \
        --group-add "$socket_gid" \
        --workdir "$home_mountpoint" \
        --volume /var/run/docker.sock:/var/run/docker.sock:rw \
        --volume "$release_volume:/release:ro" \
        --mount "type=bind,src=$home_mountpoint,dst=$home_mountpoint" \
        --mount "type=bind,src=$GOALROUTER_CODEX_HOME_HOST,dst=$GOALROUTER_CODEX_HOME_HOST,readonly" \
        --env "HOME=$home_mountpoint" \
        --env "REGISTRY_REFERENCE=$registry_reference" \
        --env "INSTALL_CODEX_HOME=$GOALROUTER_CODEX_HOME_HOST" \
        --env LIVE_INVENTORY=1 \
        --entrypoint /bin/sh \
        goalrouter-installer-smoke:local -eu -c '
            exec /bin/sh /tmp/live-subject.sh
        ' >/dev/null
else
    docker container create \
        --name "$subject_name" \
        --label "$owner_label" \
        --label "$run_label" \
        --network none \
        --user 24680:24681 \
        --group-add "$socket_gid" \
        --workdir "$home_mountpoint" \
        --volume /var/run/docker.sock:/var/run/docker.sock:rw \
        --volume "$release_volume:/release:ro" \
        --mount "type=bind,src=$home_mountpoint,dst=$home_mountpoint" \
        --env "HOME=$home_mountpoint" \
        --env "REGISTRY_REFERENCE=$registry_reference" \
        --env "INSTALL_CODEX_HOME=$home_mountpoint/.codex" \
        --env LIVE_INVENTORY=0 \
        --entrypoint /bin/sh \
        goalrouter-installer-smoke:local -eu -c '
            exec /bin/sh /tmp/live-subject.sh
        ' >/dev/null
fi
verify_container "$subject_name" || fail 'numeric subject ownership verification failed'
cat >/tmp/goalrouter-task8-live-subject.sh <<'SUBJECT'
#!/bin/sh
set -eu
        httpd -f -p 127.0.0.1:18080 -h /release &
        release_server=$!
        trap 'kill "$release_server" 2>/dev/null || :' EXIT HUP INT TERM
        mkdir -p "$HOME/.tmp"
        export TMPDIR="$HOME/.tmp"
        /bin/sh /tmp/install.sh \
            --version 1.0.5 \
            --release-base http://127.0.0.1:18080 \
            --allow-loopback-http \
            --image "$REGISTRY_REFERENCE" \
            --codex-home "$INSTALL_CODEX_HOME" \
            --yes \
            --no-path-hint \
            --skip-doctor
        "$HOME/.local/bin/goalrouter" version >/tmp/installed-version
        grep -Fxq launcher_version=1.0.5 /tmp/installed-version
        grep -Eq "^image_digest=sha256:[0-9a-f]{64}$" /tmp/installed-version
        if [ "$LIVE_INVENTORY" -eq 1 ]; then
            [ "${OPENAI_API_KEY+x}" != x ]
            inventory=$("$HOME/.local/bin/goalrouter" \
                --project "$HOME" \
                --access readonly \
                --auth-mode existing-session \
                --json models)
            printf '%s\n' "$inventory" | grep -Fq '"models"'
            printf 'installed launcher model inventory: %s\n' "$inventory"
            "$HOME/.local/bin/goalrouter" uninstall --yes
            test ! -e "$HOME/.local/bin/goalrouter"
            exit 0
        fi
        "$HOME/.local/bin/goalrouter" doctor --skip-account
        mkdir -p "$HOME/.local/state/goalrouter/runs"
        printf "# configuration-before-update\\n" >>"$HOME/.config/goalrouter/task-models.yaml"
        printf state-before-update >"$HOME/.local/state/goalrouter/runs/preserve"
        config_before=$(sha256sum "$HOME/.config/goalrouter/task-models.yaml")
        state_before=$(sha256sum "$HOME/.local/state/goalrouter/runs/preserve")
        "$HOME/.local/bin/goalrouter-install" \
            --version 1.0.5 \
            --release-base http://127.0.0.1:18080 \
            --allow-loopback-http \
            --image "$REGISTRY_REFERENCE" \
            --yes \
            --no-path-hint \
            --skip-doctor
        config_after=$(sha256sum "$HOME/.config/goalrouter/task-models.yaml")
        state_after=$(sha256sum "$HOME/.local/state/goalrouter/runs/preserve")
        test "$config_after" = "$config_before"
        test "$state_after" = "$state_before"
        "$HOME/.local/bin/goalrouter" version >/tmp/updated-version
        grep -Fxq launcher_version=1.0.5 /tmp/updated-version
        "$HOME/.local/bin/goalrouter" doctor --skip-account
        "$HOME/.local/bin/goalrouter" uninstall --yes
        test ! -e "$HOME/.local/bin/goalrouter"
        test -f "$HOME/.config/goalrouter/task-models.yaml"
        grep -Fxq state-before-update "$HOME/.local/state/goalrouter/runs/preserve"
SUBJECT
chmod 0444 /tmp/goalrouter-task8-live-subject.sh
docker container cp /workspace/scripts/install.sh "$subject_name:/tmp/install.sh"
docker container cp /tmp/goalrouter-task8-live-subject.sh "$subject_name:/tmp/live-subject.sh"
docker container start --attach "$subject_name"
docker container rm "$subject_name" >/dev/null

if [ "$live_inventory" -eq 1 ]; then
    printf 'installed launcher live inventory: models validated; runtime readonly without Docker socket or API key\n'
else
    printf 'posix installer smoke: generated install/update/uninstall roundtrip passed; no owned Docker resources remain after cleanup\n'
fi
