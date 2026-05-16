#!/bin/bash
# Finance Tracker - Container Management Script

set -e

CONTAINER_NAME="finance-tracker"
IMAGE_NAME="finance-tracker:latest"
PORT=8000

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
                podman run -d --name $CONTAINER_NAME -p $PORT:8000 -v ./data:/app/data:Z $IMAGE_NAME
            else
                podman start $CONTAINER_NAME
            fi
        else
            podman run -d --name $CONTAINER_NAME -p $PORT:8000 -v ./data:/app/data:Z $IMAGE_NAME
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
