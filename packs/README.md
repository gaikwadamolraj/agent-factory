# Domain packs

A pack is what lets one platform serve recruitment, insurance, banking and media without a code fork: it
carries a domain's vocabulary, schemas, prompts, rubrics, ontology and tool grants, while the capabilities
(document, knowledge graph, match, rules, sandbox) stay shared and domain-neutral.

Design: docs/impl_multi_domain.md (section 6 is this layout). Contract: `agent_common.pack`.

## Layout

```
packs/<pack-id>/<version>/
  pack.json                     the pack document - identity, journeys, references, tool grants, policies
  skill.md                      the Strands skill the runtime activates
  prompts/*.md                  step prompts, {{PLACEHOLDER}} templates, rendered single-pass
  ontology.json                 knowledge-graph entity/relation schema (agent_common.knowledge.ontology)
  guidance/*.md                 extraction and answering guidance
  subjects/*.schema.json        the shape of `subjects.payload` for each subject type
  criteria/*.schema.json        the criteria set a subject is graded against
  rubrics/*.rubric.json         grade vocabulary, weights, thresholds, bands - read by the rules capability
```

## Rules

- **Nothing in a pack is executable.** Only `.json .md .txt .yaml .yml .j2 .jinja .jinja2 .csv` are accepted;
  `.py`, `.sh`, `.js`, `.exe` and friends are refused, as are absolute paths and `../` traversal. Code that
  needs to run goes to the sandbox as an approved, scanned, versioned script.
- **Tool grants name tools explicitly.** A wildcard would widen the grant every time a server gained a tool.
- **The grade vocabulary lives in the rubric, not in Python.** Insurance can use
  `covered / excluded / conditional / insufficient_evidence` where ATS uses `strong / moderate / ...`, and
  that is a pack edit rather than a code change.
- **A pack cannot promote itself.** `execution.priorityTier` is a request; the effective tier is still derived
  server-side from the tenant's plan.

## Where a pack really lives

These directories are the *authoring source*. At run time a pack is a set of files on a published skill
version in registry-service, which agent-skill resolves through `resolve_skill`. They are excluded from
runtime images (`.dockerignore`).

## The ATS pack

`packs/ats/v1/` is generated from the assets that still live in code, so nothing is transcribed by hand:

```bash
uv run --directory services/common python ../../scripts/build_ats_pack.py            # regenerate + validate
uv run --directory services/common python ../../scripts/build_ats_pack.py --check    # validate only
```

Regenerate it whenever `ats_orchestrator/knowledge/guidance.py`, `knowledge/ontologies/talent_v2.json` or
`agent_skill/skills/ats_screening/v1.py` change — until the registry copy becomes the source of truth and
those modules are deleted.

## Adding a pack

1. Copy `packs/ats/v1` to `packs/<domain>/v1` and replace the content.
2. `--check` it: every reference must resolve and every rule above must pass.
3. Create a skill in the registry for the domain, upload the files as a draft version, submit, approve,
   activate. The validator runs again on write and at submit, so an invalid pack never reaches a run.
