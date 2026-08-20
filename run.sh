#!/bin/bash
# Finance Tracker - Container Management Script

set -e

CONTAINER_NAME="finance-tracker"
IMAGE_NAME="finance-tracker:latest"
PORT=8000

# Runtime configuration is passed in, never baked into the image: .env holds
# STOCKNEAR_MCP_TOKEN, and an image layer is forever. .containerignore
# excludes it from the build context for that reason.
ENV_FILE="${ENV_FILE:-.env}"

# Host browser profile whose cookies authenticate the StockNear scraper.
# Mounted read-only — the reader copies cookies.sqlite to a temp dir before
# opening it, so it never needs write access to your profile.
BROWSER_PROFILE="${BROWSER_PROFILE:-$HOME/.mozilla/firefox/rtzwitzk.default-release}"

# Assemble the `podman run` arguments shared by both start paths below.
# Populates the global RUN_ARGS array.
build_run_args() {
    RUN_ARGS=(-d --name "$CONTAINER_NAME" -p "$PORT:8000" -v ./data:/app/data:Z)

    if [ -f "$ENV_FILE" ]; then
        RUN_ARGS+=(--env-file "$ENV_FILE")
    else
        echo "Note: $ENV_FILE not found — starting on built-in defaults." >&2
        echo "      StockNear data will be unauthenticated and degrade to cache." >&2
    fi

    if [ -d "$BROWSER_PROFILE" ]; then
        # :ro,Z — both parts are load-bearing, and neither is the default.
        #
        # :ro is the container's access. The cookie reader copies
        # cookies.sqlite to a temp dir before opening it, so it never needs
        # to write here.
        #
        # :Z relabels the SOURCE on the host — persistently, and with fresh
        # per-container MCS categories on every `podman run`. Without it the
        # mount is unreadable under enforcing SELinux (a profile carries
        # user_home_t; the container gets EACCES) and the failure is quiet:
        # the reader logs "cookies.sqlite not found" and every sync fails
        # auth for what looks like an expired session.
        #
        # Note :ro does NOT prevent the relabel — it constrains the
        # container's writes, not podman's labelling of the source. Firefox
        # runs unconfined_t and keeps access to container_file_t, so this
        # does not break the browser. Undo with:
        #     restorecon -R "$BROWSER_PROFILE"
        RUN_ARGS+=(-v "$BROWSER_PROFILE:/app/browser-profile:ro,Z")
        # Must come AFTER --env-file so it overrides the host-side path that
        # .env may carry. The container sees the mount point, not the host path.
        RUN_ARGS+=(-e "STOCKNEAR_BROWSER_PROFILE_PATH=/app/browser-profile")
    else
        echo "Note: browser profile not found at $BROWSER_PROFILE" >&2
        echo "      Contract-history sync and contract quotes will fail to authenticate." >&2
        echo "      Set BROWSER_PROFILE=/path/to/profile to override." >&2
    fi
}

case "$1" in
    build)
        echo "Building container image..."
        podman build -t $IMAGE_NAME -f Containerfile .
        ;;
    start)
        echo "Starting container..."
        # Resolve the image SHA the existing container was created from (if any)
        # and compare it to the SHA of the current $IMAGE_NAME tag. `podman
        # start` reuses the original SHA — comparing tag names alone (the old
        # check did) silently kept serving the previous build even after a
        # successful rebuild. SHAs are the source of truth.
        if podman ps -a --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
            container_image_sha="$(podman inspect $CONTAINER_NAME --format '{{.Image}}' 2>/dev/null || true)"
            current_image_sha="$(podman image inspect $IMAGE_NAME --format '{{.Id}}' 2>/dev/null || true)"
            if [ -z "$container_image_sha" ] || [ -z "$current_image_sha" ] || \
               [ "$container_image_sha" != "$current_image_sha" ]; then
                echo "Recreating container — image SHA differs (current=${current_image_sha:0:12}, container=${container_image_sha:0:12})."
                podman stop $CONTAINER_NAME 2>/dev/null || true
                podman rm $CONTAINER_NAME
                build_run_args
                podman run "${RUN_ARGS[@]}" "$IMAGE_NAME"
            else
                podman start $CONTAINER_NAME
            fi
        else
            build_run_args
            podman run "${RUN_ARGS[@]}" "$IMAGE_NAME"
        fi
        echo "Application running at http://localhost:$PORT"
        ;;
    stop)
        echo "Stopping container..."
        podman stop $CONTAINER_NAME 2>/dev/null || true
        ;;
    restart)
        # Always recreate on restart — the user's intent after `build && restart`
        # is "run the new image", which `podman start` would not do. Removing
        # the container forces `start` (above) to fall through to `podman run`
        # with the freshly built image.
        echo "Restarting container (forced recreate to pick up latest image)..."
        podman stop $CONTAINER_NAME 2>/dev/null || true
        podman rm $CONTAINER_NAME 2>/dev/null || true
        $0 start
        ;;
    logs)
        podman logs -f $CONTAINER_NAME
        ;;
    clean)
        echo "Removing container and image..."
        podman stop $CONTAINER_NAME 2>/dev/null || true
        podman rm $CONTAINER_NAME 2>/dev/null || true
        podman rmi $IMAGE_NAME 2>/dev/null || true
        ;;
    status)
        podman ps -a --filter "name=$CONTAINER_NAME"
        ;;
    *)
        echo "Usage: $0 {build|start|stop|restart|logs|clean|status}"
        exit 1
        ;;
esac
