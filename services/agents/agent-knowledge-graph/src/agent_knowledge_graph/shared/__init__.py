"""Building blocks used by BOTH processes of the knowledge service:

* `episodes`        - the `knowledge_episodes` ledger and work queue (the API writes rows, the worker claims them)
* `tenancy`         - tenant graph ids (namespace + client + project)
* `graph/`          - GraphStore port + Graphiti/FalkorDB adapter
* `graph_guard`     - timeout + circuit breaker around graph calls, mapped to KNW error codes
* `graph_versions`  - per-graph change counter (worker bumps it, API cache scopes on it)
* `ontology_types`  - caller ontology -> Graphiti Pydantic types
"""
