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


def get_nodes_requested_memory() -> DefaultDict[str, float]:
    """Tally total requested memory per node from all scheduled pods."""
    pods = k8s_client.list_namespaced_pod("default").items
    requested_memory_per_node = defaultdict(float)
    # Iterate over all pods and sum up the memory requests for each node
    for pod in pods:
        # Skip pods that are not bound to a node
        if not pod.spec.node_name:
            continue
        # Get the node name
        node = pod.spec.node_name
        # Add the memory request to the node's total requested memory
        requested_memory_per_node[node] += pod_memory_request(pod)
    return requested_memory_per_node


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
    requested_memory_per_node = get_nodes_requested_memory()  # slow!!!
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
    # Watch stream
    w = watch.Watch()
    # Stream pod events; blocks until a new event arrives
    for event in w.stream(k8s_client.list_namespaced_pod, "default"):
        if (
            event["object"].status.phase == "Pending"
            and event["object"].spec.scheduler_name == scheduler_name
        ):
            # If the pod is Pending and has the correct scheduler name, try to assign it (pin) to a node
            try:
                pod = event["object"]
                optimal_node = load_balancing_assignment(pod, available_nodes())
                pod_name = pod.metadata.name
                if optimal_node is None:
                    logger.info(
                        f"No available nodes for pod {pod_name}, skipping binding.",
                    )
                    continue  # Skip binding and go to the next event
                node_name = optimal_node.metadata.name
                bind_pod_to_node(pod_name, node_name)
            except client.ApiException as e:
                logger.info(json.loads(e.body)["message"])


if __name__ == "__main__":
    main()
