#!/usr/bin/env bash
# Setup and run chaos engineering tests for trellis projection cache.
# Prerequisites: kind, kubectl, Docker Desktop
set -euo pipefail

CLUSTER_NAME="trellis-chaos"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FORWARD_PORT=18000

cleanup() {
    echo "Cleaning up..."
    kill "$PORT_FORWARD_PID" 2>/dev/null || true
    if [ "${KEEP_CLUSTER:-}" != "1" ]; then
        kind delete cluster --name "$CLUSTER_NAME" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Create kind cluster
if ! kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "Creating kind cluster: $CLUSTER_NAME"
    kind create cluster --config "$SCRIPT_DIR/kind-config.yaml"
else
    echo "Cluster $CLUSTER_NAME already exists"
fi

# Deploy SurrealDB
echo "Deploying SurrealDB..."
kubectl apply -f "$SCRIPT_DIR/surrealdb.yaml"

echo "Waiting for SurrealDB readiness..."
kubectl rollout status deployment/surrealdb --timeout=120s

# Port forward
echo "Setting up port forward on :$FORWARD_PORT..."
kubectl port-forward svc/surrealdb "$FORWARD_PORT:8000" &
PORT_FORWARD_PID=$!
sleep 3

# Verify connectivity
if curl -sf "http://localhost:$FORWARD_PORT/health" > /dev/null 2>&1 || \
   curl -sf "http://localhost:$FORWARD_PORT/version" > /dev/null 2>&1; then
    echo "SurrealDB is reachable on localhost:$FORWARD_PORT"
else
    echo "Warning: SurrealDB health check failed, proceeding anyway"
fi

# Run chaos tests
echo "Running chaos tests..."
cd "$PROJECT_ROOT"
SURREALDB_URL="ws://localhost:$FORWARD_PORT" \
    uv run python -m pytest tests/test_chaos.py -m chaos -v --tb=short "$@"

echo "Chaos tests complete."

# Keep cluster if requested
if [ "${KEEP_CLUSTER:-}" = "1" ]; then
    echo "Keeping cluster $CLUSTER_NAME (set KEEP_CLUSTER=0 to auto-delete)"
fi
