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
        if podman ps -a --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
            podman start $CONTAINER_NAME
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
        $0 stop
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
