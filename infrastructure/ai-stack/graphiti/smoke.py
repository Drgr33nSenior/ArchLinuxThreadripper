"""Offline smoke test for the Graphiti memory image, run before it is pushed.

Loads the service module without starting it (main() runs only under __main__) and
imports every graphiti_core entry point the service uses, so a lock that resolves but
does not import fails here rather than in the pod. No network, Neo4j or model access.
"""
import importlib
import importlib.util

spec = importlib.util.spec_from_file_location("memory_service", "/opt/memory/memory_service.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert callable(module.main)

for name in (
    "graphiti_core",
    "graphiti_core.cross_encoder.client",
    "graphiti_core.driver.neo4j_driver",
    "graphiti_core.edges",
    "graphiti_core.embedder.openai",
    "graphiti_core.errors",
    "graphiti_core.llm_client.config",
    "graphiti_core.llm_client.openai_generic_client",
    "graphiti_core.nodes",
):
    importlib.import_module(name)
from graphiti_core import Graphiti  # noqa: E402,F401  the class the service constructs

print("graphiti memory image smoke: ok")
