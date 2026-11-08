"""Graph store port and the Graphiti/FalkorDB adapter."""
import os

# Before any graphiti_core import: never send usage data to a third party.
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
