# agent-guardrail

Standalone guardrails agent: tells any service whether an untrusted text (uploaded document, chat message, text
from another source) tries to instruct an LLM and must be blocked. Reusable by any app: callers use
`agent_common.guardrails.client.GuardrailsClient` (MCP, service JWT `aud=agent-guardrail`, tool allow-list).

```
caller ──check_text──► NeMo Guardrails input rail "check untrusted text" (rails/*.co, no LLM call)
                         └─ action check_untrusted_text
                              ├─ pattern checks   agent_common.prompt_safety (fake system banners, overrides,
                              │                   scoring orders, 6 languages, base64-hidden orders)
                              └─ classifier       injection/jailbreak model, max score over 512-token windows
                       ◄── {blocked, score, signals, reasons, detector}
```

| What | Where |
|---|---|
| Rails (add topic / PII rails here) | `src/agent_guardrail/rails/config.yml`, `*.co` |
| Classifier | `classifier.py`: model from `CLASSIFIER_MODEL`, malicious label from the model's own `id2label`, start-up self-test (a wrong model never serves) |
| Engine | `engine.py`: NeMo input rails only (`options={"rails": ["input"]}`), no model configured |
| MCP server | `server.py`: tool `check_text`, `/healthz` (503 until the model is loaded) |
| Client for every service | `services/common/src/agent_common/guardrails/` (falls back to the pattern checks when this service is down) |

## Model
* Default `protectai/deberta-v3-base-prompt-injection-v2` (Apache-2.0, open). Measured on this machine (CPU):
  ~0.1 s per message, ~2.7 s per 7k-character resume; every attack sample 0.99+, every real resume/JD in the
  database 0.0000-0.0003.
* Llama Prompt Guard 2 (`meta-llama/Llama-Prompt-Guard-2-86M` or `-22M`): gated. Accept the Llama licence on the
  model page with the account of `HF_TOKEN` (k8s Secret key `hf-key`), then set `CLASSIFIER_MODEL`. No code change.
* The model is downloaded at start-up into `MODEL_CACHE_DIR` (a volume, so restarts reuse it).

## Settings (env)
`CLASSIFIER_MODEL`, `CLASSIFIER_THRESHOLD` (0.5), `CLASSIFIER_CHUNK_TOKENS` (512), `CLASSIFIER_CHUNK_STRIDE` (64),
`CLASSIFIER_MAX_CHUNKS` (64), `CLASSIFIER_THREADS` (2), `CLASSIFIER_CONCURRENCY` (2), `HF_TOKEN`,
`MODEL_CACHE_DIR` (/models), `RAILS_CONFIG_DIR` (empty = packaged), `ALLOWED_CALLERS`, `TOOL_POLICY`,
`AUTH_ISSUER` / `AUTH_JWKS_URL` (service tokens of the auth-service, verified locally; no secret needed).

## Run and test
```bash
uv sync
uv run pytest -q                                   # no download: fake classifier
uv run uvicorn agent_guardrail.server:app --port 8003
```
Metrics: `agent_guardrails_checks_total{source,outcome}`, `agent_guardrails_check_duration_seconds`. Blocked checks
are logged with score and reasons, never with the text.
