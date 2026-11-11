"""The deterministic half of a guardrail verdict, shared by the guardrails service and by the client's local
fallback so both judge a text identically (CLAUDE.md section 2: one implementation, used twice).

    signals = deterministic_signals(text, TextSource.CHAT_MESSAGE, rules)
    severity = severity_for(signals, classifier_over=False)
    reasons  = reasons_for(signals, classifier_over=False)

Two policies are expressed here, both overridable from settings by the service:

* **Which signals only an attack produces** (`DEFAULT_HIGH_SIGNALS`). Those make the verdict HIGH, which callers
  turn into a refusal in code. Everything else is LOW: the text goes to the model with a warning, because a lone
  ambiguous signal (a row of "=====" in a resume) must not cost a user their request.
* **Which signals only apply to what a person typed** (`DEFAULT_CHAT_ONLY_SIGNALS`). A resume may legitimately say
  "penetration testing of wifi networks" and a claim may describe an accident; a person asking the assistant to do
  those things is a different matter. Scope kinds are therefore judged on chat messages only.
"""
from agent_common.content_policy import BASE_RULE_SET, POLICY_KINDS, PolicyRuleSet, classify
from agent_common.guardrails.contracts import Severity, TextSource
from agent_common.prompt_safety import OBFUSCATED_PAYLOAD, detect

PATTERN_PREFIX = "pattern:"
POLICY_PREFIX = "policy:"
CLASSIFIER_REASON = "classifier"

# Signals that a normal document or question does not produce; a refusal in code is justified.
DEFAULT_HIGH_SIGNALS = frozenset({"role_claim", "override", "score_order", "foreign_override", "encoded_order",
                                  "hidden_encoded", *POLICY_KINDS})
# Signals judged only for text a person typed (see the module docstring).
DEFAULT_CHAT_ONLY_SIGNALS = frozenset(POLICY_KINDS)


def deterministic_signals(text: str, source: TextSource = TextSource.OTHER,
                          rules: PolicyRuleSet = BASE_RULE_SET,
                          chat_only: frozenset[str] = DEFAULT_CHAT_ONLY_SIGNALS) -> list[str]:
    """Every deterministic kind the text (and the layers hidden inside it) produces: injection signals from
    `prompt_safety.detect` plus request-scope kinds from `content_policy.classify`, with the chat-only kinds
    dropped for text that came from a document."""
    signals = list(detect(text).kinds)
    signals += [kind for kind in classify(text, rules).kinds if kind not in signals]
    if source != TextSource.CHAT_MESSAGE:
        signals = [kind for kind in signals if kind not in chat_only]
    return signals


def severity_for(signals: list[str], classifier_over: bool,
                 high_signals: frozenset[str] = DEFAULT_HIGH_SIGNALS) -> Severity:
    """HIGH when the classifier is over its threshold or a high-confidence signal fired, LOW for anything else
    that fired, NONE when nothing did."""
    if classifier_over or any(signal in high_signals for signal in signals):
        return Severity.HIGH
    return Severity.LOW if signals else Severity.NONE


def reasons_for(signals: list[str], classifier_over: bool) -> list[str]:
    """Short, user-safe reasons; never the text itself. Scope kinds keep the `policy:` prefix so a caller can tell
    "asked for something out of scope" from "tried to instruct the model"."""
    reasons = [f"{POLICY_PREFIX if signal in POLICY_KINDS else PATTERN_PREFIX}{signal}" for signal in signals]
    return reasons + ([CLASSIFIER_REASON] if classifier_over else [])


def is_blocking(signals: list[str], classifier_over: bool) -> bool:
    """Whether the text must not reach an LLM as it is. `obfuscated_payload` on its own is reported but not
    blocking: readable text inside an encoding is worth a note, and the decoded layer is judged on its own merits."""
    return classifier_over or any(signal != OBFUSCATED_PAYLOAD for signal in signals)
