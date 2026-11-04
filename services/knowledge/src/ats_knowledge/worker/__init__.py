"""Process `ats-knowledge-worker`: extracts queued episodes into the tenant graphs. Has no port and no API.

    __main__.py    entry point (python -m ats_knowledge.worker): telemetry, signals
    loop.py        poll -> claim (SKIP LOCKED) -> run up to WORKER_CONCURRENCY extractions -> drain on SIGTERM
    extraction.py  one episode: Graphiti extraction, then done / retry / failed
    container.py   wiring (database, graph store, graph versions)

Pods are scaled by KEDA on the number of queued episodes (deploy/k8s/keda), not by this code.
"""
