# Reading the logs

How to find out what a service actually did. Every query here was run against the live stack on
9 November 2026 and returned data — if one comes back empty for you, that is a real absence, not a typo.

---

## 1. Where to look

| | |
|---|---|
| **Grafana (cluster)** | `http://amol.rackspire.ai/grafana` → **Explore** → datasource **Loki** |
| **Grafana (local)** | `kubectl port-forward -n agent-infra svc/lgtm 3001:3000` → `http://localhost:3001/grafana` |
| **Grafana (compose)** | `http://localhost:3001/grafana` |
| **Raw, no Grafana** | `kubectl logs -n agent-factory deploy/<service>` |

Everything runs in one `grafana/otel-lgtm` pod in `agent-infra`: Grafana, Loki (logs), Prometheus (metrics),
Tempo (traces). Services push to it over OTLP at `lgtm.agent-infra.svc.cluster.local:4318`.

> **If Grafana shows a blank panel and the console says "Compiling or instantiating WebAssembly module
> violates the following Content Security policy directive"** — hard-refresh (Ctrl+Shift+R). Grafana's query
> editors need `wasm-unsafe-eval`, which the chart grants on `/grafana` only
> (`infra.routing.middlewares.grafanaContentSecurityPolicy`). Browsers cache CSP with the document.

---

## 2. The one thing to understand first

Logs reach Loki **over OTLP from the application**, not by scraping pod files. So the fields you filter on
are **OTLP attributes**, not Kubernetes labels. There is **no `namespace` label and no `pod` label**, and you
do **not** need `| json` — the fields are already parsed.

Only `service_name` is an indexed stream label. Everything else is structured metadata, filtered with `|`
after the stream selector.

| Field | Example | Use |
|---|---|---|
| `service_name` | `ats-orchestrator` | **the stream selector** |
| `severity_text` | `ERROR`, `WARNING`, `INFO` | severity |
| `request_id` | `466a61f2…` | **one HTTP request, across every service** |
| `client_id` / `project_id` | `1` / `2` | tenant |
| `user_id` / `username` | `2` / `acme.admin` | who |
| `chat_id` / `turn_id` | `7d7cafe1…` | one conversation / one turn |
| `code_file_path`, `code_function_name`, `code_line_number` | | where in the code |
| `deployment_environment` | `DEV` | environment |

Services currently shipping logs: `ats-orchestrator`, `agent-skill`, `agent-knowledge-graph`,
`agent-knowledge-graph-worker`, `agent-guardrail`, `auth-service`, `registry-service`. A service missing
from `service_name` values has either had no traffic or has `OTEL_ENABLED=false`.

---

## 3. Queries that work

**Every error, everywhere** — start here:

```logql
{service_name=~".+"} | severity_text="ERROR"
```

**One service:**

```logql
{service_name="agent-skill"}
{service_name="agent-skill"} | severity_text="ERROR"
```

**Follow one request across services.** The `requestId` comes back in every API error response, and this is
the single most useful query in this file — it stitches UI → orchestrator → agent → knowledge graph:

```logql
{service_name=~".+"} | request_id="466a61f29e8140fa8e7f7093d46fdf91"
```

**One tenant, one project:**

```logql
{service_name=~".+"} | client_id="1" | project_id="2"
```

**One chat, or one turn of it:**

```logql
{service_name=~".+"} | chat_id="7d7cafe154dd4e069fa5f32ffd9b632b"
{service_name=~".+"} | turn_id="ca8e1b89b97b4348825efd937312147e"
```

**A platform error code** (`{DOMAIN}-{CATEGORY}-{NNNN}`, e.g. `SCR-MIS-0003`, `FILE-TMO-0010`) — these
appear in the message, so use a line filter:

```logql
{service_name=~".+"} |= "SCR-MIS-0003"
```

**A named business event.** Services log `log_event(log, "domain.action", ...)`, so the event name is a
reliable needle — `onboard.launched`, `bulk.graded`, `run.reattached`, `security.document_flagged`,
`screening.injection_detected`, `document.extract_timeout`:

```logql
{service_name=~".+"} |= "bulk.graded"
{service_name=~".+"} |= "security.document_flagged"
```

**Slow requests** (the access log carries `duration_ms`):

```logql
{service_name=~".+"} | duration_ms > 2000
```

**Error rate per service over 5 minutes** — for a panel rather than Explore:

```logql
sum by (service_name) (count_over_time({service_name=~".+"} | severity_text="ERROR" [5m]))
```

### Reading a traceback

Python tracebacks are inside the message with escaped newlines. To read one unescaped:

```logql
{service_name="agent-skill"} | severity_text="ERROR" | line_format "{{ .event }}"
```

### Tips

- Switch Explore to **Live** to tail while you reproduce something.
- Widen the time range before concluding there are no logs — the default is often 1 hour.
- `|=` is a substring match on the line; `|` filters a structured field. Field filters are much faster.

---

## 4. Without Grafana

When Grafana itself is the thing that is broken:

```bash
kubectl logs -n agent-factory deploy/ats-orchestrator --tail=100
kubectl logs -n agent-factory deploy/agent-skill --since=10m | grep -i error
kubectl logs -n agent-factory deploy/agent-skill --previous        # the container before a crash
kubectl get events -n agent-factory --sort-by=.lastTimestamp | tail -20
```

Logs are JSON on one line. To read a traceback:

```bash
kubectl logs -n agent-factory deploy/agent-skill --tail=5 | tail -1 | sed 's/\\n/\n/g'
```

Docker compose:

```bash
docker compose logs -f ats-orchestrator
docker compose logs schema --since 5m
docker compose ps -a                      # exit codes of anything that died
```

---

## 5. Traces and metrics

Same Grafana, different datasource.

- **Tempo** — a request's span tree. Paste a `request_id`, or go from a log line to its trace: the
  log/trace correlation is why both carry the same ids.
- **Prometheus** — business metrics with deliberately low-cardinality labels (client, project, outcome —
  never free text): `agent_turns_total`, `agent_screening_duration_seconds`, `documents_read_total`,
  `injection_flags`. Dashboard: **ATS Platform Overview**.

---

## 6. What you will never find in the logs

By design, and worth knowing before you go looking (CLAUDE.md §9):

- **No passwords, tokens or secrets** — not even redacted placeholders.
- **No resume or job-description text**, and no document content. You get ids, hashes, counts and error
  codes. A screening logs that a document was flagged, never what it said.
- **No stack traces in API responses** — they are logged, never returned. The response carries a code and a
  `requestId`; the trace is what you look up with it.

So when you need to know *what* a document contained, that is a database question, not a log question.
