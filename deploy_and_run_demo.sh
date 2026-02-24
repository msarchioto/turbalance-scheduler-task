#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
step()  { echo -e "${CYAN}[STEP]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

wait_for_pod() {
    local label="$1"
    local namespace="${2:-default}"
    local timeout="${3:-120}"
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        phase=$(kubectl get pods -n "$namespace" -l "$label" -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")
        if [ "$phase" = "Running" ]; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    fail "Timed out waiting for pod with label '$label' in namespace '$namespace'"
}

wait_for_node_ready() {
    local node="$1"
    local timeout="${2:-120}"
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        ready=$(kubectl get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
        if [ "$ready" = "True" ]; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    fail "Timed out waiting for node '$node' to be Ready"
}

# ── Step 1: Clean up any existing minikube cluster ──────────────────────────
step "1/7 Deleting any existing minikube cluster..."
minikube delete --all 2>/dev/null || true
info "Clean slate."

# ── Step 2: Start minikube with 2 nodes, 2 GB each ─────────────────────────
step "2/7 Starting minikube with 2 nodes (2 GB memory each, docker driver)..."
minikube start --nodes 2 --memory 2048 --driver=docker

info "Waiting for both nodes to be Ready..."
wait_for_node_ready "minikube"
wait_for_node_ready "minikube-m02"
kubectl get nodes
info "Both nodes are Ready."

# ── Step 3: Build scheduler image and load into minikube ─────────────────────
step "3/7 Building custom-scheduler Docker image and loading into minikube..."
docker build -t custom-scheduler:latest .
minikube image load custom-scheduler:latest
info "Image built and loaded into minikube."

# ── Step 4: Deploy RBAC ─────────────────────────────────────────────────────
step "4/7 Deploying RBAC resources..."
kubectl apply -f src/k8s/scheduler-rbac.yaml
info "RBAC applied."

# ── Step 5: Deploy the custom scheduler ─────────────────────────────────────
step "5/7 Deploying custom scheduler..."
kubectl apply -f src/k8s/scheduler-deployment.yaml
info "Waiting for scheduler pod to be Running..."
wait_for_pod "app=custom-scheduler" "kube-system" 120
info "Custom scheduler is Running."

# ── Step 6: Deploy pods in order ────────────────────────────────────────────
step "6/7 Deploying pod1, pod2, pod3 sequentially..."

info "Deploying pod1 (600Mi request)..."
kubectl apply -f src/k8s/pods_engineer_task_1.yaml
sleep 5

info "Deploying pod2 (800Mi request)..."
kubectl apply -f src/k8s/pods_engineer_task_2.yaml
sleep 5

info "Deploying pod3 (600Mi request)..."
kubectl apply -f src/k8s/pods_engineer_task_3.yaml
sleep 5

info "Waiting for all pods to be Running..."
for pod in pod1 pod2 pod3; do
    elapsed=0
    while [ $elapsed -lt 60 ]; do
        phase=$(kubectl get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
        if [ "$phase" = "Running" ]; then
            break
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    if [ "$phase" != "Running" ]; then
        warn "Pod $pod is not Running (phase: $phase) after 60s"
    fi
done
info "All pods deployed."

# ── Step 7: Verification ───────────────────────────────────────────────────
step "7/7 Verification"

echo ""
echo "========================================"
echo "  Pod Placement"
echo "========================================"
kubectl get pods -o wide
echo ""

echo "========================================"
echo "  Node Resource Summary"
echo "========================================"
for node in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    echo "--- $node ---"
    kubectl describe node "$node" | grep -A 6 "Allocated resources"
    echo ""
done

echo "========================================"
echo "  Custom Scheduler Logs"
echo "========================================"
kubectl logs -n kube-system deployment/custom-scheduler --tail=50
echo ""

echo "========================================"
echo "  Scheduler Algorithm Explanation"
echo "========================================"
cat <<'EXPLANATION'
The custom scheduler (scheduler.py) implements a least-requested-memory
load-balancing strategy:

1. It watches for Pending pods whose schedulerName is "custom-scheduler".
2. For each such pod, it queries all pods in the default namespace and
   sums up the memory requests already assigned to each node.
3. It iterates over all Ready nodes and selects the node with the LOWEST
   total requested memory, provided that adding the new pod's request
   does not exceed the per-node limit (NODE_MEM_LIMIT_MB = 2048 MB).
4. If no node can fit the pod, it skips and retries on the next event.

Expected placement with 2 nodes (2048 Mi each):
  - pod1 (600Mi) -> minikube       (both nodes at 0; first in iteration)
  - pod2 (800Mi) -> minikube-m02   (0 < 600, picks least loaded)
  - pod3 (600Mi) -> minikube       (600 < 800, picks least loaded)

Result: minikube = 1200Mi, minikube-m02 = 800Mi
This spreads memory across nodes, reducing the risk that any single node
becomes overcommitted and OOM-kills pods.
EXPLANATION

info "Done! Verify the pod placement above matches the expected output."
