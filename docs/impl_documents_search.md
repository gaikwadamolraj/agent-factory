# Documents and hybrid search

* **Ingest** (`orchestrator/services/documents`): resume (PDF) or JD (PDF/TXT/MD or pasted text) -> type checked from
  the bytes -> text extracted -> de-duplicated per tenant + kind + SHA-256 -> original file in the `BlobStore`
  (Postgres today; add S3/Azure/GDrive by implementing `BlobStore` and registering it for `BLOB_BACKEND`) ->
  chunked (1200 chars, 200 overlap) -> embedded with the LM Studio embedding model (nomic Q8_0, auto-selected) ->
  `document_chunks` rows (`embedding vector`, generated `tsv tsvector`, tenant columns).
* **Hybrid search** (`ChunkRepository.hybrid_search`): two rankings over the chat's documents only -
  pgvector cosine distance and Postgres full-text (`websearch_to_tsquery`, `ts_rank_cd`) - fused with Reciprocal
  Rank Fusion (k=60). If embeddings are unavailable, keyword search still answers.
* **Use**: the assistant's `search_documents(query, source=resume|jd|both)` tool; answers quote the passages.
* **Limits**: 10 MB per file, 25 MB per request (Next.js proxy + orchestrator `BodySizeLimitMiddleware`).
* **Preview**: `GET /api/v1/chats/{id}/documents/{resume|jd}/file|text` (tenant-scoped, `nosniff`, no-store).
