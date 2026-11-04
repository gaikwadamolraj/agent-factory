# Local LLM (LM Studio)

> Services no longer call LM Studio directly: they go through the LLM gateway, which routes to LM Studio and the
> vLLM host by alias. Model names and addresses live in `deploy/k8s/base/llm-gateway/litellm-config.yaml`
> (see [llm_gateway.md](llm_gateway.md)). This page is about running LM Studio itself.

LM Studio serves `qwen2.5-14b-instruct-1m` and the embedding model on the host (`http://host.docker.internal:1235/v1`).

| Role | Model | Set in |
|---|---|---|
| Chat / screening / extraction (behind gateway aliases) | `qwen2.5-14b-instruct-1m` | backend `qwen25_14b` in `litellm-config.yaml` |
| Embeddings | nomic embed (Q8_0, auto-selected) | discovered from `GET /v1/models` |

Previous model: `meta-llama-3.1-8b-instruct` (keep it downloaded until Qwen has been verified, see the revert plan below).

## 0. Faster models on this machine (GPU + mixture-of-experts) - in progress

**Machine:** AMD Ryzen AI 9 HX 370, Radeon 890M (integrated GPU, shared memory), 61.6 GB RAM, no NVIDIA GPU.
LM Studio runs llama.cpp on **Vulkan**, so the 890M can run models.

**Why change:** on a laptop chip, generation speed is limited by memory bandwidth (~120 GB/s), not compute. A
dense 14B (qwen2.5-14b, 9 GB) reads all 9 GB per token, about 7 tokens/s today. A **mixture-of-experts (MoE)** model
reads only ~3B parameters per token: similar or better quality, several times faster.

| Candidate | Type | Size (Q4) | Fits fully in 32 GB GPU memory |
|---|---|---|---|
| `Qwen3-30B-A3B-Instruct-2507` (recommended) | MoE, 3.3B active | ~18.6 GB | yes (~22 GB with 64k context) |
| `gpt-oss-20b` (runner-up) | MoE, 3.6B active | ~12 GB | yes (also fits 16 GB) |

### 0.1 One-time: give the GPU 32 GB (reboot required)
1. AMD Software: Adrenalin Edition → **Performance → Tuning → Variable Graphics Memory → 32 GB** → reboot.
   (Options are 16 or 32 GB. 16 GB fits only gpt-oss-20b.)
2. This memory is taken from Windows (about 30 GB stays for Windows, WSL/k3d, VS Code, browser). Keep it free:
   ```bash
   docker compose down          # the k3d cluster is the real stack; data volumes are kept
   lms unload qwen2.5-14b-instruct-1m   # do not keep the old 14B loaded next to the new model
   ```
3. LM Studio → Settings → Runtime: keep **llama.cpp Vulkan** selected.

### 0.2 Load a model fully on the GPU
Download both models in the LM Studio UI, then get their exact keys:
```bash
lms ls                                   # copy the model keys shown under LLM
lms ps                                   # only the embedding model should be loaded
lms load <qwen3-30b-a3b key> --gpu max -c 65536 --parallel 4 --ttl 86400 -y
```
In the LM Studio UI (model → load settings) also turn on **Flash Attention** and set **K/V cache quantization to
Q8_0** (halves the memory per parallel request). Check Task Manager → Performance → GPU: *Dedicated GPU memory*
should grow by ~20 GB (model fully on the GPU). If it stays low, the model is still on the CPU.

| Flag | Meaning |
|---|---|
| `--gpu max` | full offload to the GPU (`off` = CPU only, `0.5` = half) |
| `-c 65536` | context shared by all parallel slots (4 x 16k) |
| `--parallel 4` | concurrent requests; set the backend's `max_parallel_requests` in `litellm-config.yaml` to the same |

### 0.3 Benchmark before switching (same cases for every model)
```bash
# LM Studio directly (one big model loaded at a time); synthetic resume, no personal data
uv run --no-project --with openai python scripts/llm_benchmark.py --base-url http://localhost:1235/v1 \
    --models <qwen3-30b-a3b key> --runs 3 --out bench_qwen3_30b.json
lms unload <qwen3-30b-a3b key>
lms load <gpt-oss-20b key> --gpu max -c 65536 --parallel 4 --ttl 86400 -y
uv run --no-project --with openai python scripts/llm_benchmark.py --base-url http://localhost:1235/v1 \
    --models <gpt-oss-20b key> --runs 3 --out bench_gpt_oss_20b.json
```
Cases: answers from excerpts (name, location, current employer), choosing a tool instead of guessing, JSON-schema
roles, ontology-style extraction; plus time to first token and tokens/s.

Baseline measured 2026-11-04: **qwen3-4b** (vLLM-MLX, Mac): 15/18, 62 tokens/s, 1.8 s to first token; its one miss is
extraction (takes the headline instead of the current job title). qwen2.5-14b: see `bench_*.json` once run.

### 0.4 Switch the gateway to the winner
In `deploy/k8s/base/llm-gateway/litellm-config.yaml`: change the `qwen25_14b` backend's `model:` to
`openai/<winner key>` (rename the anchor if you like), set `max_input_tokens` (context per slot - 4096) and
`max_parallel_requests`, bump `LLM_CONFIG_VERSION` in `deploy/k8s/base/llm-config/kustomization.yml`, then
`./deploy.sh orchestrator`. Services keep using the same aliases.

## 1. Start

```bash
lms server start --bind 0.0.0.0 --port 1235

lms unload meta-llama-3.1-8b-instruct   # only the old chat model; embedding models stay loaded
lms load qwen2.5-14b-instruct-1m --parallel 5 -c 65536 --ttl 86400 -y
lms load <your-nomic-embed-model> -y # only if `lms ps` does not already show it
lms ps                               # expect qwen2.5-14b-instruct-1m + the embedding model
```

| Flag | Meaning |
|---|---|
| `--parallel 5` | concurrent requests; matches `LLM_MAX_CONCURRENCY=5` on the orchestrator |
| `-c 65536` | context length. The "1m" model supports much more, but every token of context costs VRAM |
| `--ttl 86400` | auto-unload after 24 h idle |
| `-y` | no prompts |

The model key must match the `model:` of the backend in `litellm-config.yaml` exactly. `lms ls` lists `qwen2.5-14b-instruct-1m` (8.99 GB) and
`meta-llama-3.1-8b-instruct` (5.73 GB), so the keys used here are correct. Unload by name only, never with
`--all`: `--all` also unloads the embedding model, and document search then falls back to keyword-only.

`lms ls` = downloaded models; `lms ps` = loaded models. `lms unload` only accepts a loaded model's identifier as
shown by `lms ps`. "Model Not Found" on unload means the model is not loaded, so skip that step. If a model was
loaded twice it may appear as `<name>:2`; unload it by that exact identifier.

## 2. Verify before using the UI

```bash
curl -s http://localhost:1235/v1/models          # qwen2.5-14b-instruct-1m must be listed
curl -s http://localhost:1235/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-14b-instruct-1m","messages":[{"role":"user","content":"Reply with {\"ok\":true} only"}],"temperature":0}'
```

Then restart the stack so the services pick up the new name, and run one screening:

```bash
docker compose up --build -d agent orchestrator
docker compose logs -f agent    # look for agent.screening_completed, not agent.model_format_error
```

Cached analyses are keyed by model name, so switching models never serves old Llama results.

**Kubernetes:** model changes are made in the gateway config; its ConfigMaps are generated with a content hash, so a
deploy rolls the gateway (and, for `ats-llm-config`, every LLM user) automatically.
A pod still asking for the old model makes LM Studio auto-load it next to Qwen (seen: Llama loaded with a 131k
context). Memory then runs out, Qwen slows to about 2 tokens/s and requests fail with HTTP 500. `lms ps` shows
what is loaded; unload anything that should not be there.

## 3. If it does not fit in memory

Symptoms: `lms load` fails, the load is very slow, or requests time out (`LLM-TMO-0001`). Try these in order:

```bash
lms unload qwen2.5-14b-instruct-1m
lms load qwen2.5-14b-instruct-1m --parallel 3 -c 32768 --ttl 86400 -y   # also set LLM_MAX_CONCURRENCY=3
lms load qwen2.5-14b-instruct-1m --parallel 2 -c 16384 --ttl 86400 -y   # also set LLM_MAX_CONCURRENCY=2
```

A screening prompt uses well under 16k tokens, so 16k context is enough for normal use.

## 4. Revert plan (back to Llama 3.1 8B)

Revert if Qwen still fails after step 3, or if screenings regularly produce `agent.model_format_error` or `LLM-TMO-0001`.

1. Swap the model in LM Studio:
   ```bash
   lms unload qwen2.5-14b-instruct-1m   # embedding models stay loaded
   lms load meta-llama-3.1-8b-instruct --parallel 5 -c 65536 --ttl 86400 -y
   lms ps
   ```
2. Point the backend at the old model, in one file: `deploy/k8s/base/llm-gateway/litellm-config.yaml`,
   backend `qwen25_14b` -> `model: openai/meta-llama-3.1-8b-instruct` (and `max_input_tokens` to its context).
   Bump `LLM_CONFIG_VERSION` in `deploy/k8s/base/llm-config/kustomization.yml` so cached results of the old model
   are not reused.
3. Apply: Kubernetes `./deploy.sh orchestrator` (the gateway and LLM users roll automatically);
   Docker Compose `docker compose restart llm-gateway`.

No service code or service config changes: services only know aliases. Extraction needs reliable structured output;
an 8B model is weak at that, so keep `ats-extraction` on a stronger backend if you revert screening.
