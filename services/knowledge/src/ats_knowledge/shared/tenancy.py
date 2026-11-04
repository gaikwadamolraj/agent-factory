"""Tenant isolation: every (namespace, client, project) gets its own FalkorDB graph.

The graph id is built here, from validated values only (namespace pattern + positive integer ids), never from
free text. Graphiti also stamps it as `group_id` on every node and edge, a second fence inside the graph.
"""
import re
from dataclasses import dataclass
from typing import Optional

from ats_common.knowledge.contracts import TenantRef

_GRAPH_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}__c\d{1,10}_p\d{1,10}$")
_CLIENT_IN_GRAPH_ID = re.compile(r"__c(\d{1,10})_p\d{1,10}$")


@dataclass(frozen=True)
class TenantGraph:
    namespace: str
    client_id: int
    project_id: int

    @classmethod
    def of(cls, ref: TenantRef) -> "TenantGraph":
        return cls(ref.namespace, ref.client_id, ref.project_id)

    @property
    def graph_id(self) -> str:
        """e.g. talent__c1_p2 (FalkorDB graph name and Graphiti group_id)."""
        graph_id = f"{self.namespace}__c{self.client_id}_p{self.project_id}"
        if not _GRAPH_ID.match(graph_id):   # defence in depth: inputs are validated at the edge already
            raise ValueError("invalid tenant graph id")
        return graph_id

    @staticmethod
    def client_of(graph_id: str) -> Optional[int]:
        """Client id encoded in a graph id (None for ids that are not tenant graphs)."""
        match = _CLIENT_IN_GRAPH_ID.search(graph_id)
        return int(match.group(1)) if match else None
