# Roadmap: candidate intelligence

Phase 1 (done): shared knowledge service. Profiles and screening outcomes go into a per-tenant graph; the chat can
query it; answers are grounded and semantic-cached. Everything below plugs into the same service. New sources
become new episodes (`source_type` = `github`, `linkedin`, `message`, `email`) for the same person.

## Principles for every phase

* **One agent per source, one interface**: a `SourceConnector` (fetch → normalise to text → `ingest_episode`) so
  GitHub, LinkedIn and later sources are interchangeable and testable without the network.
* **Consent and lawful basis first**: record per candidate and channel *why* we may process or contact
  (`candidate_channels.consent`, timestamp, source). No consent, no fetch, no message.
* **Durable jobs**: connector and outreach work runs through queued jobs with retries and leases (same pattern as
  `knowledge_episodes`), never inside a chat turn.
* **Erasure everywhere**: every stored item carries a `source_ref` so `forget_source` and table deletes remove it.

## Phase 2: profile enrichment (`services/connectors`)

| Step | What | Notes |
|---|---|---|
| 2.1 Identity | `candidates` table (tenant columns, display name, primary resume document) and `candidate_channels` (type: github, linkedin, portfolio, email, phone, whatsapp; handle; verified; consent) | Deterministic extraction from resume text (URL / email / phone patterns), not the LLM. Gives the graph a stable candidate id instead of `document:N` |
| 2.2 GitHub agent | Official REST/GraphQL API with a GitHub App token: public repos, languages, contribution and commit activity, PRs and reviews, stars. Summarised into an episode ("contributed N commits to X in language Y over Z months") | Respect rate limits (ETag caching, conditional requests); only public data; re-fetch on a schedule (activity is time-sensitive, and Graphiti's temporal facts keep history) |
| 2.3 LinkedIn | **Decision needed.** Scraping breaks LinkedIn's terms; the official API exposes almost nothing about third parties. Options: (a) the candidate consents to an import (OAuth sign-in or uploading their own data export), (b) a licensed data provider. Build the connector interface; plug in (a) first | Never store credentials of third parties |
| 2.4 Fit signals | Code activity and profile facts as extra facts in the graph; screening stays deterministic and may *show* them as evidence | No automated rejection from activity data |

## Phase 3: communication (`services/outreach`)

| Step | What | Notes |
|---|---|---|
| 3.1 Email | `MailProvider` interface (SMTP / SES / Microsoft Graph); templates versioned like prompts; sequences (invite, reminder, follow-up) with send windows | Unsubscribe link, bounce handling, per-tenant sender domains (SPF/DKIM) |
| 3.2 WhatsApp | Meta WhatsApp Business Cloud API: pre-approved template messages to opted-in candidates; inbound webhook with signature verification | Opt-in recorded in `candidate_channels`; 24-hour service window rules |
| 3.3 Conversation log | `conversations` / `messages` tables (tenant columns, channel, direction, status) | Replies (availability, notice period, salary expectation) become graph episodes (`source_type=message`) |
| 3.4 Recruiter-in-the-loop | Drafts by the LLM, sent after recruiter approval, until trust is earned per template | Audit log of who approved what |

## Phase 4: demand matching (about 1,000+ profiles)

1. **Freeze `talent` ontology v1** (see the ontology guide) and re-ingest profiles, so every candidate has
   comparable `Skill`, `Role`, `Certification` nodes and attributes.
2. **Bulk ingest** through the existing queue (one resume ≈ 5 minutes on a local 14B model). Plan LLM capacity:
   more GPU / endpoints, or a hosted model for bulk runs.
3. **Brief → requirements** with the screening agent's existing JD analysis (atomic, quoted requirements).
4. **Recall**: `search_knowledge(answer=false, ontology=talent v1)` per requirement plus vector recall over
   candidate nodes, giving a candidate pool.
5. **Rank** the pool with the screening agent's deterministic rubric (`screening/scoring.py`), with evidence.
6. **Supply vs demand analytics**: which requirements are scarce in the client's pool; trend per skill over time
   (temporal facts).

## Decisions open

* LinkedIn data source (2.3).
* Hosted LLM for bulk extraction and outreach drafting, or local only?
* Retention periods per data type (profiles, activity data, messages) per client contract.
