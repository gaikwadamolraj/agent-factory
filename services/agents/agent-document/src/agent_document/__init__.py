"""agent-document: a reusable internal agent that reads uploaded documents (PDF / Word / Excel).

It extracts text, tables and metadata, can injection-scan the text through the shared guardrails service, and can
chunk + embed the text for search. It is exposed only over MCP (zero-trust service tokens); no public routes.
"""
