# Knowledge graph basics

A short primer for anyone working on or with the shared **knowledge service** (`services/knowledge`).
How we built it: [impl_knowledge_graph.md](impl_knowledge_graph.md). How to describe your data:
[knowledge_ontology_guide.md](knowledge_ontology_guide.md).

## Why a graph?

A resume is text. Search over text (our pgvector + keyword search) finds *passages*. A knowledge graph stores
*facts as connections*, for example:

```
(Priya Raman) -[WORKED_AT {from: 2021}]-> (Northwind Logistics)
(Priya Raman) -[USES]-> (Apache Kafka)
(Priya Raman) -[HOLDS]-> (AWS Certified Data Engineer)
```

You can then ask questions that span documents and people: *"who has led a migration and holds a cloud
certification?"*, *"which candidates worked at the same company?"*. Matching a brief against 1,000 profiles
(roadmap phase 4) relies on this.

## Vocabulary

| Term | Meaning | Example |
|---|---|---|
| **Entity** (node) | A thing: person, skill, company, certification | `Apache Kafka` |
| **Relation** (edge) | A typed link between two entities, written as a fact sentence | `Priya uses Apache Kafka` |
| **Fact** | The sentence an edge stands for; what search returns and answers cite | |
| **Episode** | One piece of raw text added to the graph. Everything in the graph traces back to an episode | a resume, a GitHub summary |
| **Ontology** | Optional list of entity and relation *types* to extract | `Person`, `Skill`, `HAS_SKILL` |
| **Guidance** | Plain-language instructions from the caller: what to extract, or how to answer | "extract roles with dates" |
| **Namespace** | The use case that owns a graph | `talent` |
| **Graph id** | One isolated graph = namespace + client + project | `talent__c1_p2` |

## Graphiti in one minute

[Graphiti](https://github.com/getzep/graphiti) is the open-source library that builds and searches the graph.
For each episode it:

1. **extracts entities** with the LLM (guided by our guidance and, if given, the ontology),
2. **resolves duplicates**: "Kafka" and "Apache Kafka" in two resumes become one node,
3. **extracts relations** as facts between those entities,
4. **tracks time**: every fact has `valid_at` / `invalid_at`. A newer episode that contradicts an old fact
   marks the old one invalid instead of deleting it, so history stays answerable,
5. **embeds** names and facts so search can find them by meaning.

Search combines keyword (BM25) and vector similarity and fuses the rankings (reciprocal rank fusion).

This takes several LLM calls per episode, so it is slow on a local model: about 1.5 minutes for two sentences
and many minutes for a full resume on Qwen 2.5 14B. That is why ingestion runs in a background worker, and why
screening never waits for the graph.

## FalkorDB

The graph database. It runs as a Redis module, holds many named graphs, and Graphiti maps each `group_id` to its
own graph. We use one graph per tenant per use case, so tenants are physically separate.

## Semantic cache

Recruiters ask the same things in different words ("does she know Kafka?" / "Kafka experience?"). The semantic
cache (RedisVL on Redis 8) stores answers with the question's embedding. A new question close enough in meaning,
for the **same graph, same graph version and same guidance**, gets the stored answer without a search or an LLM
call. Any change to the graph bumps its version, so stale answers are never served.

## Grounded answers

`search_knowledge` asks the LLM to answer **only from the facts the search returned** and to cite their ids. Code
then drops citations of facts that were not retrieved. An answer with no valid citation becomes "no information".
The graph can be wrong if extraction was wrong, but an answer cannot add facts the graph does not hold.

## What it is not

* Not the source of truth for documents (Postgres holds those) or for screening scores (the screening agent
  computes them deterministically).
* Not a place for secrets or protected characteristics: the talent guidance tells the LLM never to infer them.
