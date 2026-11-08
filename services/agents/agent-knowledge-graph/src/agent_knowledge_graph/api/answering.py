"""Grounded answers: the LLM may only use the facts the graph search returned, and must cite them.

Code then checks the citations: ids that are not among the retrieved facts are dropped, and an answer that cites
nothing real is replaced by NOT_FOUND. So an answer can never introduce a fact the graph does not hold.
The caller's guidance shapes tone and focus; it cannot widen what the model is allowed to use.
The LLM call itself (gateway, alias per client policy, timeout, breaker) is agent_common.llm.
"""
import json
from typing import Optional

from agent_common.knowledge.contracts import EntityHit, Fact
from agent_common.llm import Purpose, structured_completion
from pydantic import BaseModel, Field

from agent_knowledge_graph import metrics
from agent_knowledge_graph.api.prompts import ANSWER_PROMPT

NOT_FOUND = "The knowledge graph has no information that answers this question."
ANSWER_MAX_TOKENS = 1200


class _Answer(BaseModel):
    answer: str = Field(description="Answer using only the facts; empty if they do not answer the question")
    cited_fact_ids: list[str] = Field(default_factory=list, description="ids of the facts the answer uses")


def render_facts(facts: list[Fact], entities: list[EntityHit]) -> str:
    """Facts and entity summaries as the numbered context block the prompt expects."""
    lines = [f"[{f.fact_id}] {f.fact}" + (" (no longer true)" if f.invalid_at else "") for f in facts]
    lines += [f"(entity) {e.name} [{', '.join(e.labels) or 'Entity'}]: {e.summary}" for e in entities if e.summary]
    return "\n".join(lines)


def ground(answer: _Answer, facts: list[Fact]) -> tuple[str, list[str]]:
    """Keep only citations of retrieved facts; no valid citation means no answer."""
    known = {f.fact_id for f in facts}
    cited = [fact_id for fact_id in dict.fromkeys(answer.cited_fact_ids) if fact_id in known]
    if not cited or not answer.answer.strip():
        return NOT_FOUND, []
    return answer.answer.strip(), cited


class Answerer:
    async def answer(self, question: str, guidance: str, facts: list[Fact], entities: list[EntityHit],
                     client_id: Optional[int] = None) -> tuple[str, list[str]]:
        """(answer, cited fact ids). Raises AppError LLM-* from agent_common.llm."""
        if not facts:
            return NOT_FOUND, []
        prompt = (ANSWER_PROMPT.replace("{{GUIDANCE}}", guidance or "Answer plainly and briefly.")
                  .replace("{{FACTS}}", render_facts(facts, entities))
                  .replace("{{QUESTION}}", json.dumps(question)))
        result = await structured_completion(Purpose.ANSWER, prompt, _Answer, client_id=client_id,
                                             op="llm.knowledge_answer", max_tokens=ANSWER_MAX_TOKENS)
        metrics.answers_total.add(1)
        return ground(result, facts)
