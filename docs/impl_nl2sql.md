# NL2SQL and the semantic layer — assessment of the NVIDIA auto-ontology prototype, and the target design

**Status:** assessment + design. Not built. Journey J5 in docs/impl_multi_domain.md; v3.0 `dataops`.
**Date:** 7 November 2026
**Source material:** `services/agents/agent-nvidia-auto-ontology-nl2sql/` (531 lines, copied in by the user).

---

## 1. What the prototype is

Four files wrapping a database-introspection → LLM → semantic-catalog → SQL-validation loop:

| File | Does |
|---|---|
| `ontology_engine.py` (264) | `profile_database` (SQLAlchemy inspector: tables, columns, FKs, sample rows), `compile_ontology` (LLM or heuristic), `search_ontology` (token overlap), `validate_and_execute_sql` |
| `mcp_server.py` (114) | FastMCP server exposing `compile_ontology_from_db`, `register_certified_term`, `search_ontology`, `validate_and_execute_sql` |
| `models.py` (40) | `CertifiedTerm`, `SemanticRelationship`, `OntologyCatalog`, `ValidationResult` |
| `agent_client.py` (67) | Strands agent consuming the MCP server |

## 2. What is genuinely valuable — keep this

1. **`profile_database`** — clean SQLAlchemy introspection into a schema profile. Reusable almost verbatim.
2. **The semantic-catalog model.** `CertifiedTerm{term_id, business_name, definition, source_table,
   mapped_columns, sql_formula, synonyms, certified}` + `SemanticRelationship{from/to table+column, join_type}`
   + `table_summaries`. This is a good semantic layer and it maps cleanly onto v3.0's `semantic_models` /
   `semantic_entities` / `semantic_metrics`. Adopt the shape.
3. **`_heuristic_compile`** — a deterministic fallback that builds terms and joins from columns and foreign
   keys with no LLM at all. Keep it as the default and treat the LLM pass as enrichment. It makes the service
   testable offline and cheap.
4. **Provenance on validation** — reporting which certified terms a query resolved to. That is the right
   instinct and matches our grounding discipline.
5. **The separation** compile / register term / search / validate+execute is the right tool decomposition.

## 3. What must not ship — the read-only gate does not hold

`validate_and_execute_sql` (`ontology_engine.py:205`) is the security boundary, and it is bypassable:

```python
if not sql_lower.startswith("select") and not sql_lower.startswith("with"):
    errors.append("Security Gate: Only read-only SELECT / WITH queries are permitted.")
```

| # | Problem | Why it matters |
|---|---|---|
| 1 | **Data-modifying CTEs pass.** `WITH gone AS (DELETE FROM customers RETURNING *) SELECT * FROM gone;` starts with `with` | PostgreSQL executes it. The gate deletes data while reporting "read-only". |
| 2 | **No statement-separator check.** `SELECT 1; DROP TABLE x` | Depends only on the driver's multi-statement behaviour |
| 3 | **Table allow-list is a regex over raw SQL**, `re.search(rf"\b{table}\b", sql_lower)` | A table name inside a string literal or comment satisfies it; a real table referenced via a view or quoted identifier does not |
| 4 | **No read-only transaction**, no `SET TRANSACTION READ ONLY`, no role separation | The connection's own privileges are the only real limit |
| 5 | **No statement timeout, no cost guard** | One query can pin the customer's production database |
| 6 | **Row cap applied after fetch** — `rows[:100]` after materialising every row | Unbounded memory on a large result |
| 7 | **Raw `db_url` with credentials passed as a tool argument** | Credentials flow through LLM-adjacent code and into logs/traces |
| 8 | **Ontology catalog is a JSON file on disk** | No tenancy, no RLS; every client shares one catalog |
| 9 | **LLM called directly at NVIDIA with an API key** | Violates CLAUDE.md §2: LLM access only via `agent_common.llm` + the gateway, by `Purpose` alias |
| 10 | Sync SQLAlchemy, `create_engine` per call | No pooling; blocks the event loop in an async service |

Points 1-5 are the serious ones. **Prompting is not a control and neither is a prefix check** — the query has
to be parsed, and the database identity has to be read-only regardless of what the parser concludes.

## 4. Target design — `agent-nl2sql`

Rebuild as a normal platform service (`services/agents/agent-nl2sql`, package `agent_nl2sql`, MCP + the usual
`agent_common` middleware, auth, tenancy, errors, resilience, metrics). The prototype becomes the reference
implementation for the compile and introspect paths.

### 4.1 Defence in depth, in order

1. **Registered data source, never a URL from a caller.** Tools take a `data_source_id`; the connection string
   comes from `agent_common.secrets`. A `db_url` argument is never accepted.
2. **A read-only database identity**, created by the customer, with `SELECT` only on allow-listed objects.
   This is the control that still works when everything above it fails.
3. **Parse, do not pattern-match.** Use `sqlglot` to parse into an AST, then: exactly one statement; the root
   is `SELECT`/`WITH`; **no `Insert`/`Update`/`Delete`/`Merge` node anywhere in the tree** (this is what kills
   the CTE bypass); no DDL; no `INTO`; no locking clauses; every referenced table and column resolved against
   the allow-list from the AST, not from a regex; no unapproved functions (`pg_read_file`, `dblink`, `COPY`,
   `lo_import`, ...).
4. **Read-only transaction**: `BEGIN READ ONLY`, `SET LOCAL statement_timeout`, `SET LOCAL idle_in_transaction_session_timeout`.
5. **`LIMIT` injected into the query** at the AST level, not applied to the result list.
6. **Per-tenant concurrency cap and rate limit.**
7. **Human approval** for sensitive datasets or expensive plans (v3.0 §13; our existing approval flow).
8. **Record everything**: SQL hash, redacted SQL, semantic-model version, policy version, approver, row count,
   duration, result artifact reference.

### 4.2 Storage, in our conventions

JSON file store → tenant-scoped tables (BIGSERIAL `id` + `external_id`, `client_id`, `AuditMixin`, RLS):

```
data_sources               connector, secret ref, classification, residency, status
schema_snapshots           per data source, versioned, schema fingerprint
schema_objects             tables/columns/FKs/sensitivity, from the introspector
semantic_models            + semantic_model_versions (published, approved)
semantic_terms             CertifiedTerm, versioned with the model
semantic_relationships     SemanticRelationship, versioned with the model
data_query_policies        allow/deny objects, max_rows, timeout, read_only (always true), approval rules
nl2sql_requests            + candidates, approvals, executions  (the audit trail)
```

The semantic model gets the **same lifecycle as a skill version** — draft → in_review → published → archived,
approved by a human, pinned per run. An uncertified term can never be used in a published query path.

### 4.3 Where the LLM goes

Through `agent_common.llm` by `Purpose` alias, configured in the gateway. Two calls, both optional:
- **compile** (enrichment over the heuristic catalog) — `Purpose.EXTRACTION`
- **generate candidate SQL** — a new purpose alias, temperature 0, schema-constrained output

The heuristic compiler stays the default so the service works with no LLM at all.

### 4.4 Fit with the rest of the platform

- It is a **shared capability**, not a domain service: banking, media and ATS all use it (docs/impl_multi_domain.md §4, J5).
- The semantic model is a **pack asset** — `semantic/` in the pack directory — so a domain ships its own
  certified terms.
- Tabular ingestion (CSV / RDBMS, existing_comment §19-21) lands rows, then J5 answers questions over them;
  no spreadsheet is ever stuffed into a prompt.
- `agent-nl2sql` is in the `agents:` values block, internal only, no route, its own KEDA pool.

## 5. What to do with the copied directory

`services/agents/agent-nvidia-auto-ontology-nl2sql/` does not follow the service conventions (no `pyproject.toml`,
no `src/` layout, no Dockerfile, no uv lock, flat imports, name is not `agent-<what it does>`). It is a
prototype, not a service, and `deploy.sh` does not know about it.

Keep it as a reference until `agent-nl2sql` exists, then delete it. Do not wire it into the chart, the compose
file or `deploy.sh` in its current form — in particular, **do not expose `validate_and_execute_sql` to any
agent** until §4.1 is implemented.

## 6. Build order

J5 is late on purpose (docs/impl_multi_domain.md §9, step 8). It depends on `subjects`, the pack contract and
the approval flow. When it starts:

1. `agent-nl2sql` service skeleton + registered data sources + secrets.
2. Introspection and the heuristic compiler (port from the prototype), snapshots and schema objects.
3. The `sqlglot` validator and the read-only execution harness — **with tests for every bypass in §3**.
4. Semantic model lifecycle + admin-ui screens, reusing the skill-version pattern.
5. LLM enrichment and candidate generation through the gateway.
6. Approval flow for sensitive datasets; the full audit trail.
