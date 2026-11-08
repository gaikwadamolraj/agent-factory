"""Process `agent-knowledge-graph`: the MCP server other services call.

    server.py          MCP tools + zero-trust middleware   (entry: uvicorn agent_knowledge_graph.api.server:app)
    service.py         use cases: ingest (queue only), get_episode, search, forget, cancel
    semantic_cache.py  RedisVL answer cache, scoped per tenant graph + graph version
    answering.py       grounded answer from graph facts (+ citation check)
    container.py       wiring

It never extracts: ingest only queues a row in `knowledge_episodes`; the worker process does the extraction.
"""
