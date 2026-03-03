import json
import os
import re
import logging
import sys

from typing import DefaultDict
from collections import defaultdict

from kubernetes import client, config, watch
from kubernetes.client import V1Pod, V1Node

# Authenticate using the pod's service account token
config.load_incluster_config()
k8s_client = client.CoreV1Api()

# Pods must set schedulerName to this value to be handled by this
scheduler_name = "custom-scheduler"

# Basic logging configuration
logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(scheduler_name)



def available_nodes() -> list[V1Node]:
    """Return only nodes whose Ready condition is True."""
    return [
        node
        for node in k8s_client.list_node().items
        if any(
            condition.status == "True" and condition.type == "Ready"
            for condition in node.status.conditions
        )
    ]


def bind_pod_to_node(pod_name: str, node_name: str, namespace: str = "default") -> None:
    """Tell the API server to place this pod on the given node."""
    # Creates a V1Binding object and posts it to the Kubernetes API,
    # which tells the API server "this pod should run on this node."
    # Same behavior as the default scheduler.
    target = client.V1ObjectReference(kind="Node", api_version="v1", name=node_name)
    meta = client.V1ObjectMeta(name=pod_name)
    body = client.V1Binding(target=target, metadata=meta)
    k8s_client.create_namespaced_binding(namespace, body, _preload_content=False)


def parse_memory_quantity_to_bytes(q: str) -> float:
    """Convert a K8s memory string like '256Mi' or '1Gi' to bytes."""
    m = re.match(r"^([0-9.]+)([KMGTE]i)?$", q)
    num = float(m.group(1))
    suf = m.group(2) or ""
    mult = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Ei": 1024**6,
    }.get(suf, 1)
    return num * mult


def pod_memory_request(pod: V1Pod) -> float:
    """Sum the memory requests across all containers in a pod."""
    return sum(
        (
            parse_memory_quantity_to_bytes(c.resources.requests.get("memory", 0.0))
            if c.resources and c.resources.requests
            else 0.0
        )
        for c in pod.spec.containers
    )


# In-memory node memory accounting, updated incrementally via the watch stream
# instead of re-listing all pods on every scheduling decision.
_node_memory: DefaultDict[str, float] = defaultdict(float)
_pod_assignments: dict[str, tuple[str, float]] = {}  # pod uid -> (node_name, bytes)


def _update_pod_tracking(event: dict) -> None:
    """Incrementally update per-node memory totals from a single watch event.

    Uses a pop-then-insert pattern that handles ADDED, MODIFIED, and DELETED
    events uniformly without special-casing:
      1. Remove the pod's previous accounting (if any).
      2. If the pod still exists and is assigned, record the new accounting.
    """
    event_type = event["type"]
    pod: V1Pod = event["object"]
    uid = pod.metadata.uid

    # Step 1: Undo previous accounting — subtract the old memory from the old
    # node so that moves, updates, and deletions all start from a clean slate.
    prev = _pod_assignments.pop(uid, None)
    if prev:
        _node_memory[prev[0]] -= prev[1]

    # Step 2: Record new accounting — only for pods that still exist (not
    # DELETED) and have been assigned to a node (node_name is set by the
    # kubelet after binding).
    if event_type != "DELETED" and pod.spec.node_name:
        mem = pod_memory_request(pod)
        _pod_assignments[uid] = (pod.spec.node_name, mem)
        _node_memory[pod.spec.node_name] += mem


def get_nodes_requested_memory() -> DefaultDict[str, float]:
    """Return current in-memory snapshot of requested memory per node."""
    return defaultdict(float, _node_memory)


def get_node_available_memory_bytes() -> float:
    """
    Returns artificial memory limit (in bytes) per node used by the scheduler.

    We intentionally do NOT use node.status.allocatable["memory"], because when
    Minikube runs with the Docker driver, the container runtime does not necessarily
    enforce node memory limits on some OSes. As a result, the allocatable value is not a
    reliable hard limit. Instead, we rely on an explicit limit configured via
    the NODE_MEM_LIMIT_MB environment variable.
    """
    mem_mb_str = os.environ.get("NODE_MEM_LIMIT_MB", "2048")
    mem_mb = float(mem_mb_str)
    return mem_mb * 1024 * 1024

# Core scheduling logic
def load_balancing_assignment(pod: V1Pod, nodes: list[V1Node]) -> V1Node | None:
    """Pick the node with the lowest memory usage that can still fit the pod."""
    memory_request = pod_memory_request(pod)
    requested_memory_per_node = get_nodes_requested_memory()
    pod_name = pod.metadata.name
    logger.info(f"Assigning pod {pod_name} with memory request {memory_request}:")
    optimal_node = None
    optimal_node_requested_memory = None
    # Iterate over all nodes and find the one with the lowest memory usage (optimal_node_requested_memory)
    for node in nodes:
        node_name = node.metadata.name
        available_memory = get_node_available_memory_bytes()
        node_requested_memory = requested_memory_per_node[node_name]
        # If the node does not have enough memory to fit the pod
        # (already used memory + requested memory > available memory), skip it
        if node_requested_memory + memory_request > available_memory:
            continue
        # If the node has less memory usage than the current optimal node, update the optimal node
        if (
            optimal_node is None
            or optimal_node_requested_memory > node_requested_memory
        ):
            optimal_node, optimal_node_requested_memory = node, node_requested_memory
    if optimal_node:
        logger.info(f"Optimal node for pod {pod_name}: {optimal_node.metadata.name}")
    return optimal_node


# Main loop, long-living process that runs until stopped
def main():
    """Watch for Pending pods and assign them to the least-loaded node."""
    w = watch.Watch()
    # PATCH: Solve race condition
    # Note: This set (scheduled_pods) grows unboundedly. If pods are frequently
    # created/deleted over a long scheduler lifetime, you may want to evict
    # entries when a pod leaves Pending (e.g., on DELETED events or when phase
    # changes to Running/Succeeded/Failed). For short-lived or test schedulers
    # this is fine as-is.
    scheduled_pods: set[str] = set()
    for event in w.stream(k8s_client.list_namespaced_pod, "default"):
        _update_pod_tracking(event)
        pod = event["object"]
        if (
            pod.status.phase == "Pending"
            and pod.spec.scheduler_name == scheduler_name
            and pod.metadata.name not in scheduled_pods
        ):
            try:
                optimal_node = load_balancing_assignment(pod, available_nodes())
                pod_name = pod.metadata.name
                if optimal_node is None:
                    logger.info(
                        f"No available nodes for pod {pod_name}, skipping binding.",
                    )
                    continue
                node_name = optimal_node.metadata.name
                bind_pod_to_node(pod_name, node_name)
                scheduled_pods.add(pod_name)
            except client.ApiException as e:
                logger.info(json.loads(e.body)["message"])


if __name__ == "__main__":
    main()
