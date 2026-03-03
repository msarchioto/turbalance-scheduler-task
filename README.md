# Turbalance Scheduler

A custom Kubernetes scheduler that balances pod placement across nodes based on
memory requests, minimizing the risk of OOM-killed pods.

Google Slides Presentation: [link](https://docs.google.com/presentation/d/1fN2kMt4K23wxD2zZY2ZXGUbH5dPRP0zBFsd4uw6Hbm4/edit?usp=sharing)

## Table of Contents

- [Repository Structure](#repository-structure)
- [How the Scheduler Works](#how-the-scheduler-works)
  - [Expected Placement (2 nodes, 2048 Mi each)](#expected-placement-2-nodes-2048-mi-each)
  - [Scheduler Patch 1: Solve race condition](#scheduler-patch-1-solve-race-condition)
  - [Scheduler Patch 2: `get_nodes_requested_memory()` performance](#scheduler-patch-2-get_nodes_requested_memory-performance)
- [Kubernetes Manifests](#kubernetes-manifests)
  - [`src/k8s/scheduler-rbac.yaml`](#srck8sscheduler-rbacyaml)
  - [`src/k8s/scheduler-deployment.yaml`](#srck8sscheduler-deploymentyaml)
- [Dockerfile](#dockerfile)
- [All-in-one Demo Script](#all-in-one-demo-script)
  - [Step-by-step breakdown](#step-by-step-breakdown)
- [Prerequisites](#prerequisites)
- [Running](#running)
- [`deploy_and_run_demo.log`](#deploy_and_run_demolog)

## Repository Structure

```
.
├── Dockerfile
├── deploy_and_run_demo.sh
├── deploy_and_run_demo.log
├── README.md
└── src/
    ├── scheduler.py
    └── k8s/
        ├── scheduler-rbac.yaml
        ├── scheduler-deployment.yaml
        ├── pods_engineer_task_1.yaml   (pod1 — 600 Mi request)
        ├── pods_engineer_task_2.yaml   (pod2 — 800 Mi request)
        └── pods_engineer_task_3.yaml   (pod3 — 600 Mi request)
```

## How the Scheduler Works

`src/scheduler.py` implements a **least-requested-memory** scheduling strategy.
It replaces the default Kubernetes scheduler for any pod whose
`spec.schedulerName` is set to `custom-scheduler`.

1. **Watch loop** — The scheduler opens a watch stream on all pods in the
   `default` namespace. When a pod appears with `status.phase == "Pending"` and
   `spec.schedulerName == "custom-scheduler"`, it triggers the placement logic.

2. **Memory accounting** — For every Ready node, the scheduler sums up the
   `resources.requests.memory` values of all pods already bound to that node.
   The per-node capacity is **not** read from the node's allocatable resources
   (which can be unreliable under minikube's Docker driver on some OSes);
   instead it comes from the `NODE_MEM_LIMIT_MB` environment variable
   (default 2048, i.e. 2 GiB).

3. **Node selection** — The scheduler iterates over all Ready nodes and picks
   the one with the **lowest total requested memory** whose remaining headroom
   can still fit the new pod (`existing_requests + pod_request <= limit`).
   If no node qualifies, the pod is skipped and will be retried on the next
   watch event.

4. **Binding** — Once a target node is chosen, the scheduler creates a
   `v1.Binding` object that assigns the pod to that node, the same API call
   the default scheduler uses.

### Expected Placement (2 nodes, 2048 Mi each)

| Order | Pod  | Request | Node State Before         | Assigned To   | Reason                                      |
|-------|------|---------|---------------------------|---------------|----------------------------------------------|
| 1     | pod1 | 600 Mi  | node1=0, node2=0          | node1         | Both empty; first node in iteration wins     |
| 2     | pod2 | 800 Mi  | node1=600, node2=0        | node2         | 0 < 600 — node2 is least loaded              |
| 3     | pod3 | 600 Mi  | node1=600, node2=800      | node1         | 600 < 800 — node1 is least loaded            |

Final state: node1 = 1200 Mi, node2 = 800 Mi. Memory is spread across both
nodes, reducing the chance that either one becomes overcommitted and starts
killing pods.

### Scheduler Patch 1: Solve race condition

The initial implementation had a race condition in the watch loop. The
Kubernetes watch stream can emit multiple events for the same pod (ADDED,
MODIFIED, etc.) while it is still in `Pending` phase. Because the scheduler
processed every matching event independently, it attempted to bind the same pod
multiple times. The first binding succeeded, but subsequent attempts hit a
conflict error:

```
2026-02-24 18:27:37,013 INFO Assigning pod pod1 with memory request 629145600.0:
2026-02-24 18:27:37,013 INFO Optimal node for pod pod1: minikube
2026-02-24 18:27:37,114 INFO Assigning pod pod1 with memory request 629145600.0:
2026-02-24 18:27:37,114 INFO Optimal node for pod pod1: minikube-m02
2026-02-24 18:27:37,116 INFO Operation cannot be fulfilled on pods/binding "pod1": pod pod1 is already assigned to node "minikube"
2026-02-24 18:27:37,126 INFO Assigning pod pod1 with memory request 629145600.0:
2026-02-24 18:27:37,126 INFO Optimal node for pod pod1: minikube-m02
2026-02-24 18:27:37,128 INFO Operation cannot be fulfilled on pods/binding "pod1": pod pod1 is already assigned to node "minikube"
```

The same pattern repeated for every pod (pod2, pod3). The duplicate attempts
are harmless (the API server rejects them) but they add unnecessary API calls
and. More importantly, the second attempt can compute a *different* optimal
node than the one that actually won the race — making the logs confusing.

**Fix:** A `scheduled_pods: set[str]` is maintained in the watch loop. Once a
pod is successfully bound, its name is added to the set and all future events
for that pod are skipped before any scheduling logic runs.

**IMPORTANT NOTE**: This set grows unboundedly. If pods are frequently created/deleted over a long scheduler lifetime, we may want to evict entries when a pod leaves Pending (e.g., on DELETED events or when phase changes to Running/Succeeded/Failed). For short-lived or test schedulers this is fine as-is.

### Scheduler Patch 2: `get_nodes_requested_memory()` performance

The original `get_nodes_requested_memory()` called
`k8s_client.list_namespaced_pod("default")` on **every scheduling decision**,
iterating over all pods in the namespace to sum their memory requests per node.
This is an O(n) API round-trip plus O(n) iteration each time, where n is the
total number of pods. As the cluster grows, this becomes a bottleneck: each
scheduling decision pays the full cost of listing and scanning every pod, even
though the vast majority of pod states have not changed since the last call.

**Fix:** Replace the per-call API list with **incremental, event-driven
tracking**. Two module-level structures maintain the state:

- `_node_memory: DefaultDict[str, float]` — running total of requested memory
  per node.
- `_pod_assignments: dict[str, tuple[str, float]]` — maps each pod UID to its
  assigned node and memory request, so the old value can be subtracted when a
  pod moves or is deleted.

A new helper, `_update_pod_tracking(event)`, is called for **every** watch
event in the main loop (not just Pending pods). It uses a **pop-then-insert**
pattern that handles all event types (ADDED, MODIFIED, DELETED) uniformly
without special-casing:

1. **Remove old** — pop the pod's previous `(node, bytes)` entry and subtract
   the bytes from the node's running total. Harmless no-op for first-time pods.
2. **Add new** — if the pod is not DELETED and has a `node_name`, compute its
   memory request, record the assignment, and add the bytes to the node total.

This keeps `_node_memory` accurate at all times with O(1) work per event.

**Worked example:**

```
State: _node_memory = {node-A: 512Mi}, _pod_assignments = {uid-1: (node-A, 512Mi)}

Event: MODIFIED — uid-1 moved to node-B, requests 256Mi
  1. pop uid-1 → (node-A, 512Mi);  node-A: 512-512 = 0
  2. insert uid-1 → (node-B, 256Mi); node-B: 0+256 = 256Mi

Event: DELETED — uid-1
  1. pop uid-1 → (node-B, 256Mi);  node-B: 256-256 = 0
  2. skip (DELETED)

Event: ADDED — uid-2 on node-A, requests 1Gi
  1. pop uid-2 → None (first time, no-op)
  2. insert uid-2 → (node-A, 1Gi);  node-A: 0+1Gi = 1Gi
```

`get_nodes_requested_memory()` now returns a **shallow copy**
(`defaultdict(float, _node_memory)`) instead of making an API call, reducing
the scheduling hot path from O(n) API + iteration to an O(k) dict copy
(k = number of nodes, typically very small). The copy is necessary for two
reasons:
(1) `defaultdict` auto-creates missing keys on read. For example, if only
`node1` has pods, `_node_memory` is `{"node1": 600.0}`. When
`load_balancing_assignment` reads `_node_memory["node2"]`, the `defaultdict`
returns `0.0` **and inserts** `"node2": 0.0` into the dict as a side effect.
After iterating all nodes, `_node_memory` would be littered with zero-valued
entries that `_update_pod_tracking` never created. The copy absorbs these
insertions instead
(2) the copy provides snapshot isolation, guaranteeing
that the values the caller iterates over cannot shift mid-loop if the design
ever moves to async or threaded event processing.

## Kubernetes Manifests

### `src/k8s/scheduler-rbac.yaml`

This file contains three Kubernetes resources that grant the scheduler the
API permissions it needs to operate inside the cluster:

- **ServiceAccount** (`custom-scheduler-sa`, namespace `kube-system`) —
  The identity under which the scheduler pod runs. Kubernetes uses this
  account to authorize its API calls.

- **ClusterRole** (`custom-scheduler-role`) — Defines the exact set of
  permissions the scheduler requires:
  | Resource        | Verbs                  | Why                                                       |
  |-----------------|------------------------|-----------------------------------------------------------|
  | `pods`          | get, list, watch       | Watch for new Pending pods and read their memory requests  |
  | `nodes`         | get, list              | Discover which nodes are Ready and available               |
  | `pods/binding`  | create                 | Bind a pod to a chosen node (the scheduling action itself) |
  | `bindings`      | create                 | Alternative binding resource used by the Kubernetes API    |
  | `events`        | create, patch, update  | Emit events that show up in `kubectl describe pod`         |

- **ClusterRoleBinding** (`custom-scheduler-rolebinding`) — Connects the
  ServiceAccount to the ClusterRole, activating the permissions cluster-wide.
  A *Cluster*RoleBinding (rather than a namespaced RoleBinding) is needed
  because the scheduler must read nodes and pods/bindings across namespaces.

### `src/k8s/scheduler-deployment.yaml`

A standard Kubernetes **Deployment** that runs the scheduler as a single-replica
pod inside the `kube-system` namespace:

- **`image: custom-scheduler:latest`** with **`imagePullPolicy: Never`** —
  The image is built locally inside minikube's Docker daemon, so Kubernetes
  must not try to pull it from a remote registry.

- **`serviceAccountName: custom-scheduler-sa`** — Runs under the service
  account created by the RBAC manifest, giving it the permissions listed above.

- **`NODE_MEM_LIMIT_MB=2048`** (environment variable) — Tells the scheduler
  to treat every node as having 2048 MiB of schedulable memory, regardless of
  what the node reports as allocatable. This works around minikube's Docker
  driver not always enforcing accurate memory limits.

## Dockerfile

The `Dockerfile` packages the scheduler into a minimal container image:

1. **`FROM python:3.11-slim`** — Starts from a lightweight Python base image
   to keep the image small.
2. **`COPY src/scheduler.py .`** — Copies the scheduler script into the
   container's working directory.
3. **`RUN pip install --no-cache-dir kubernetes`** — Installs the official
   Kubernetes Python client, the only runtime dependency.
4. **`CMD ["python", "scheduler.py"]`** — Runs the scheduler when the
   container starts.

The image is built locally and then loaded into minikube via
`minikube image load`, which distributes it to all nodes in the cluster.
This approach is compatible with multi-node minikube clusters (unlike
`minikube docker-env`, which only works with single-node setups).


## All-in-one Demo Script 

`deploy_and_run_demo.sh`

An all-in-one demo shell script that sets up the minikube cluster, deploys everything, and
verifies the result. It requires a working `minikube` and `docker` installation
already present in the environment. It uses `set -euo pipefail` so any failure
aborts the script immediately.

### Step-by-step breakdown

| Step | What it does | Why |
|------|-------------|-----|
| **1 — Delete existing cluster** | Runs `minikube delete --all` to remove any leftover cluster. | Guarantees a clean, reproducible starting state. |
| **2 — Start minikube** | `minikube start --nodes 2 --memory 2048 --driver=docker` then waits for both nodes (`minikube`, `minikube-m02`) to report Ready. | Creates the two-node, 2 GiB-per-node cluster the task requires. The Docker driver is the most portable option for WSL2 and Linux hosts. |
| **3 — Build the Docker image** | Builds the image locally (`docker build -t custom-scheduler:latest .`) and loads it into minikube (`minikube image load custom-scheduler:latest`). | `minikube image load` distributes the image to all nodes, which is required for multi-node clusters (`minikube docker-env` is incompatible with multi-node). The tag `latest` with `imagePullPolicy: Never` prevents Kubernetes from trying to pull externally. |
| **4 — Deploy RBAC** | `kubectl apply -f src/k8s/scheduler-rbac.yaml` | Creates the ServiceAccount, ClusterRole, and ClusterRoleBinding before the scheduler pod starts, so it has permissions from the moment it boots. |
| **5 — Deploy the scheduler** | `kubectl apply -f src/k8s/scheduler-deployment.yaml`, then polls until the scheduler pod reaches `Running`. | The scheduler must be up and watching before any workload pods are created; otherwise they would stay Pending indefinitely. |
| **6 — Deploy workload pods** | Applies `pods_engineer_task_1.yaml`, `_2.yaml`, `_3.yaml` one at a time with a 5-second pause between each, then waits for all three to reach `Running`. | The pauses ensure the scheduler processes each pod in the intended order (pod1, pod2, pod3), which matters for deterministic placement. |
| **7 — Verification** | Prints pod placement (`kubectl get pods -o wide`), node resource usage, and the scheduler's own logs. Finishes with a plain-text explanation of the algorithm and expected placement. | Lets you confirm at a glance that every pod landed on the expected node and that the custom scheduler (not the default one) performed the assignments. |

## Prerequisites

- Docker
- minikube
- kubectl

## Running

```bash
./deploy_and_run_demo.sh
```

The script takes roughly 2-3 minutes (mostly minikube startup). When it
finishes, look at the "Pod Placement" and "Custom Scheduler Logs" sections in
the output to verify that the scheduler placed each pod as expected.

## `deploy_and_run_demo.log`

Full execution log of `deploy_and_run_demo.sh`, captured from a successful run
against a two-node minikube cluster.