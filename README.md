# agent-factory

Profile skill vs JD: a multi-tenant recruiter chat assistant that screens resumes against job descriptions for
**any industry**. Coding agents: read [CLAUDE.md](CLAUDE.md) first.

Architecture, service names and namespaces: **[docs/platform.md](docs/platform.md)**.

```
browser ─► Traefik ─► ats-ui (proxy /ats/api/backend)
                          ├─► auth-service            login / refresh / users; JWKS + service tokens for services
                          └─► ats-orchestrator ──MCP──► agent-skill (skills: ATS screening)
                                                     ├─MCP──► agent-compute ──► agent-sandbox
                                                     ├─MCP──► agent-knowledge-graph ◄── agent-knowledge-graph-worker
                                                     └─MCP──► agent-guardrail
  infra (namespace agent-infra): postgres, redis, falkordb, llm-gateway (LiteLLM), lgtm (Grafana)
```

| Path | What it is |
|---|---|
| [services/common](services/common) | Shared lib `agent_common`: config, logging + OTEL, errors, resilience, Redis keys, auth, tenant tables, middlewares, LLM, agent clients |
| [services/platform/schema](services/platform/schema) | Database: creates `ats_platform`, Alembic migration, demo seed (Helm hook Job) |
| [services/platform/auth-service](services/platform/auth-service) | Login, refresh, logout, user management, user + service tokens (ES256), JWKS |
| [services/ats/ats-orchestrator](services/ats/ats-orchestrator) | ATS API `/api/v1`: chats, documents, screenings, SSE turns, abort, LangGraph assistant |
| [services/agents/agent-skill](services/agents/agent-skill) | Runs skills (Strands); ATS screening is a skill |
| [services/agents/agent-compute](services/agents/agent-compute), [services/agents/agent-sandbox](services/agents/agent-sandbox) | Runs agent-written code in an isolated sandbox |
| [services/agents/agent-knowledge-graph](services/agents/agent-knowledge-graph) | Knowledge graph per tenant (Graphiti + FalkorDB) behind MCP, plus the extraction worker |
| [services/agents/agent-guardrail](services/agents/agent-guardrail) | Prompt-injection check for any untrusted text (NeMo rail + classifier) |
| [services/ats/ats-ui](services/ats/ats-ui) | Next.js UI (`ats-ui`) |
| [deploy](deploy) | Helm chart `deploy/helm/agent-factory` (namespaces `agent-factory` + `agent-infra`), deployed with `./deploy.sh` |
| [docs](docs) | [platform.md](docs/platform.md) (start here), [access_and_onboarding.md](docs/access_and_onboarding.md), [local_llm.md](docs/local_llm.md), [llm_gateway.md](docs/llm_gateway.md), [security_auth.md](docs/security_auth.md), [security_llm.md](docs/security_llm.md), [impl_agent_platform.md](docs/impl_agent_platform.md) (skills, workflow runs, artifacts), knowledge graph [basics](docs/knowledge_graph_basics.md) / [ontology guide](docs/knowledge_ontology_guide.md), [KEDA](docs/keda_autoscaling.md), `impl_*.md` for complex flows - not shipped in images |

## Run locally

```bash
lms server start --bind 0.0.0.0 --port 1235             # LM Studio, reachable from containers
lms load qwen3-30b-a3b-instruct-2507 --gpu max -c 65536 --parallel 4 --ttl 86400 -y
lms load text-embedding-nomic-embed-text-v1.5@q8_0 --ttl 86400 -y
./scripts/local_env.sh                                  # creates .env with generated keys (never commit .env)
docker compose up --build
```
UI http://localhost:3000/ats (users `acme.recruiter`, `acme.admin`, `globex.recruiter`, password `Demo@12345`),
admin console http://localhost:3002/agent-factory (skills, versions, domain packs, tool catalogue; admin role),
Grafana http://localhost:3001/grafana. Model setup and pitfalls: [docs/local_llm.md](docs/local_llm.md).

## Deploy to k3d

```bash
./deploy.sh init --generate-secrets   # once: namespaces, generated keys, list of the other Secret keys
./deploy.sh                 # build all images, helm upgrade --install (infra in agent-infra, services in agent-factory)
```
Details, secret keys and access: [deploy/README.md](deploy/README.md).

## Demo script

1. Sign in as `acme.recruiter`, pick the **Engineering hiring** project, press **New chat**.
2. Paste a JD, attach a resume PDF, press **Screen candidate**: the timeline shows each agent step live.
3. Ask follow-ups ("Which must-haves are missing?", "What should I ask about X?").
4. Start another screening and press **Stop**: the turn ends at once and the agent's LLM call is cancelled.
5. Open Grafana → Dashboards → ATS → **ATS Platform Overview**: active users, who did what, latency, LLM calls, aborts, errors by code.
6. Sign in as `globex.recruiter`: Acme's chats are invisible (tenant isolation).

## Error codes

`{DOMAIN}-{CATEGORY}-{NNNN}` (e.g. `WFL-CAN-0001`), defined in `agent_common.errors` and each service's `errors.py`.
Every error response: `{"error": {code, message, hint, retryable, retryAfter, requestId}}` - never a stack trace.


# Tomorrows plan 
1. allow to upload 1000 profile
2. UI enahcnement proper look and feel
3. allow user to find the profile for JD and get top listed 
    1. find appropriate profile then run the check again JD and score it 
  4. as of now if ask for any other question like write a code is doing so we should limit with guard rail that only resones related with application should response instead of someting else. handle this too
5. agent-common - its services\common\src\agent_common - src\agent_common
6. logging - corealtion id - from user rquets to till to affected down system - global tracking journey - name the bset 
   also if chat between agents then create anew chat id so that we can control iteratoin additional id 
   for the UI - add chat id in the browser so direct links works same for other use case possible if
7. BYOA - concept we have to make the mechanism either use there login or if ours then for long running jobs we have to allow refresh token - think here, they may be MCP(http-steam or sse if old) or a2a or may be http call also not sure
8. use all the jwt standards so we can compliant for all - like iss public url for jwks to vlaidate if they want
9. svc to svc token use token for created service only not for all - a -> b not b -> c only b -> a.
10. JWKS public url - cache the content so no load system once we rotate keys refresh tings
11. secrets not mount to file - # A `*_FILE` variable pointing at a readable file wins (k8s mounts a Secret as a file for larger values). - runtime only fetch 
12. set up this on our env atleast: OpenBaoSecretProvider
13. access policy - allowed for time slots - auto reset -temp system
14. notify by email, text msg whats slack team etc via agent - notification to UI chat 
15. when we say mcp tools as api wrapper expose then we have to apply the tool policyh on the api too 
16. BodySizeLimitMiddleware - same config from gatway so one location change will reflect both locatoin - middleware + gatway 
17. ats.auth - not only for ats so rename to proper name agent_factory is our main and ats is the one of the client. agent_llm_calls_total - one more name agent_llm_call_duration_seconds 
18. agent-document - we have to think of the pdf file contain the table chart image - strategy for the same, allow orchestrator to pass prompt what he want the data. if tabular data then csv file etc.
19. if such data comes in pdf file like tabular large data and user is going to ask the ques like average of the data how to do - dicsuss this with me - my idea we can store the jsonb in pg at this stage and then use nl2sql with data.
20. if file is csv of rdbms data then we have to create a table and store the data and then using nl2sql return data. get the proper dteails for relation between tables, colmn names details for good quality response. while ingestion make the data sense if value not provided means get some sample group type data then create own(i am doing like dbt lake house - genie ) - very big idea and need to create a sepreate agent for such scenaios - seperate agent - not in immedate plan but future
21. we need to think to create sepreate agent for this so we can enhance insted of in document agent - smeantic query sample for each table and common - also we can think use any trained model for the sql query (future). 
22. while creating the vector we should not hit every time to vector get from semantic cache - refresh data should support with versoin - 
23. db - we dont have who crated - created_by updated_by full audit for all the tables , to show public ids - uuid so no one cane predit the sequence or pattern - those ids we are providing and most secur client project user etc 
role: str = Field(default="recruiter", max_length=32) - we moving to common service to use proepr user types that we are going to create email -make mandotry so that we can keep dteails allow dummy .
still cant see agent-factory admin related tables here?
in ats tables
created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field() - repeated so crease base model and use in all - common audit table allow soft delete only metadata - 
jsonb column in all tables so we can keep key-value data without touching db for small things - for all the tables
services\agents\agent-skill\src\agent_skill\memory\models.py - jsonb so we can put summarisation of chat with bg job 
create index appropritately for each table 
access group users - think again all aspects and see what other people build user access model and do better way. follow IAM role policy kind of mechanism really liked the same.
create cache at repo level with index number so if user updated or deleted update cache - cache pattern per client not overall unless its agent_factory level - maintain this pattern

24. nvidia or gvisor sandbox agent are beter instead of doing the code and we can sacle with keda? 
 at this stage allow to run the python code only but get the py or runtime params files as data - like html render with json data or other things if the wnat to run.
25. agent_skill - this is ats related agent. think we have to create a common - all progress should come form temporal so track from there. 
26. handle the state at he orchetrator level - agent-skill is doing to checkin - pass the chat history to him in prompt for direct chat - 
27. chat summrisation we have to do instead of increased things again and again - k8s job or celery job based on the scale on keda -    