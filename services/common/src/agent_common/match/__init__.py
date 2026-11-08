"""`agent-match`: the domain-neutral half of matching a subject against a set of criteria.

What is shared across every industry is the *discipline*, not the vocabulary: extract criteria from a
source, extract what a subject's documents state, grade one against the other, and refuse any claim that
cannot be traced back to stored text. That is this package. What a job requirement, a coverage clause or a
KYC control means stays in the service that owns that industry (docs/impl_multi_domain.md section 2.2).

Pair it with `agent_common.rules`, which turns the grades this produces into a score under the pack's
rubric. Neither package calls an LLM: a model proposes criteria and grades somewhere else, and everything
here checks that proposal against the source.
"""
from agent_common.match.grounding import (Attribute, Criterion, GradeClaim, GroundingPolicy, prematch,
                                          verify_attributes, verify_criteria, verify_grades)
from agent_common.match.text import (DEFAULT_OVERLAP, DEFAULT_STOPWORDS, key_words, normalize,
                                     phrase_in_text, same, snippet, supported_by, term_in_text)

__all__ = ["DEFAULT_OVERLAP", "DEFAULT_STOPWORDS", "Attribute", "Criterion", "GradeClaim", "GroundingPolicy",
           "key_words", "normalize", "phrase_in_text", "prematch", "same", "snippet", "supported_by",
           "term_in_text", "verify_attributes", "verify_criteria", "verify_grades"]
