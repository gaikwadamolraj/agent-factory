"""What an assistant may be *asked to do*: request-scope policy, configurable per industry.

`agent_common.prompt_safety` answers "does this text try to act as instructions?". This module answers the other
half: "is this a request this product is here to serve?". Both are deterministic (no LLM) and both run on the text
*and* on every layer `prompt_safety.decoded_layers` finds inside it, so an encoded question is judged like a plain
one.

Three kinds, in increasing order of how much context they need:

* `harmful_request`   - asks for operational help with something harmful or illegal (breaking into networks or
                        accounts, malware, weapons, drug synthesis, self-harm). Always out of scope, whatever the
                        industry, and not excused by on-topic words in the same message.
* `code_request`      - asks the assistant to write or debug software (any language). This is an assistant for one
                        business journey; it does not generate code.
* `off_topic_request` - asks it to produce or explain something unrelated to its journey (a security policy, an
                        architecture, an essay, a travel plan, "how do I secure my app with zero trust").

**Industry rules are data, not code.** The base rule set below is domain-agnostic (CLAUDE.md section 10): it knows
nothing about recruiting or insurance. Each domain pack carries a `guardrails/policy.json` that adds its own
vocabulary, which is what keeps the industries apart - "claim", "endorsement", "deductible" are on-journey for the
insurance pack and mean nothing to the ATS pack, and neither pack has to be a code change. A rule set is merged as
`base + pack`, so a pack can widen the vocabulary but never remove a harmful-request rule.

    rules = BASE_RULE_SET.merge(rule_set_from_document(pack.json("guardrails/policy.json")))
    report = classify(text, rules)      # report.kinds -> ("off_topic_request",)

File format (`guardrails/policy.json`, validated by `rule_set_from_document`):

    {
      "onJourney":      ["candidate", "resume", "job description"],   # phrases that mark a message as on-journey
      "blockedTopics":  {"off_topic_request": ["underwriting fraud scheme"]},
      "allowedTopics":  ["python", "kubernetes"]                      # never off-journey for this industry
    }
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Optional

from agent_common.prompt_safety import decoded_layers, normalize

HARMFUL_REQUEST = "harmful_request"
CODE_REQUEST = "code_request"
OFF_TOPIC_REQUEST = "off_topic_request"
POLICY_KINDS = (HARMFUL_REQUEST, CODE_REQUEST, OFF_TOPIC_REQUEST)

MAX_RULE_PHRASES = 500          # per rule set, so a pack cannot turn a check into an unbounded scan
MAX_PHRASE_CHARS = 120

# A request at all: something is being asked for or asked about. Without one of these the text is a statement
# (a pasted resume, a claim description) and the scope kinds below do not apply.
_ASK = re.compile(r"\b(?:write|generate|create|produce|compose|draft|build|develop|implement|code|program|script|"
                  r"give|show|send|provide|make|design|prepare|fix|debug|refactor|optimi[sz]e|convert|port|"
                  r"translate|explain|teach|tell|walk\s+me|guide|help\s+me|how\s+(?:do|can|should|to)|steps?\s+to|"
                  r"tutorial|example\s+of|can\s+you)\b", re.IGNORECASE)

# Operational help with something harmful or illegal. Deliberately about *doing* it, so that a profile or a claim
# describing security work ("penetration testing", "fraud investigation") is not caught.
_HARMFUL = re.compile(r"\b(?:crack|bypass|brute[\s-]?force|bruteforce|hack(?:ing)?\s+into|break\s+into|"
                      r"gain\s+access\s+to|steal|exfiltrate|intercept|sniff|spoof|phish|keylog\w*)\b"
                      r"[^.\n]{0,60}?\b(?:wi-?fi|wifi|wlan|wpa\d?|network|router|firewall|password|passwords|"
                      r"credential|credentials|account|accounts|database|server|mailbox|session|token|encryption|"
                      r"2fa|mfa|otp|cctv|camera|atm|card|identity)\b|"
                      r"\b(?:wi-?fi|wifi|password|credential|account|router|server|database)\b[^.\n]{0,40}?"
                      r"\b(?:crack|cracking|bypass|brute[\s-]?force|hack|hacking)\b|"
                      r"\b(?:write|build|create|generate|make)\b[^.\n]{0,40}?\b(?:malware|ransomware|virus|worm|"
                      r"trojan|rootkit|botnet|keylogger|exploit|payload|backdoor|ddos|dos\s+attack|sql\s*injection|"
                      r"phishing\s+(?:page|site|email|kit))\b|"
                      r"\b(?:make|build|synthesi[sz]e|manufacture|obtain|buy)\b[^.\n]{0,40}?\b(?:bomb|explosive|"
                      r"ied|napalm|thermite|nerve\s+agent|ghost\s+gun|untraceable\s+(?:gun|firearm)|meth\w*|"
                      r"fentanyl|cocaine|heroin|mdma|lsd)\b|"
                      r"\b(?:how\s+to|ways?\s+to|best\s+way\s+to)\b[^.\n]{0,40}?\b(?:kill\s+(?:myself|yourself)|"
                      r"commit\s+suicide|self[\s-]?harm|hurt\s+myself|overdose)\b",
                      re.IGNORECASE)

# Software: the artefact asked for, or a language named as the medium.
_CODE_ARTEFACT = re.compile(r"\b(?:code|codebase|program|programme|script|snippet|boilerplate|function|method|"
                            r"class|module|library|package|algorithm|pseudo-?code|unit\s+tests?|regex|"
                            r"stored\s+procedure|query|sql|api\s+endpoint|endpoint|microservice|web\s*(?:site|page|"
                            r"app)|mobile\s+app|crawler|scraper|bot|dockerfile|manifest|helm\s+chart|"
                            r"terraform|playbook|pipeline\s+config\w*)\b", re.IGNORECASE)
_CODE_LANGUAGE = re.compile(r"\b(?:in|using|with|w/)\s+(?:java|python|c\+\+|c#|csharp|javascript|typescript|"
                            r"node(?:\.js)?|golang|go|rust|ruby|php|kotlin|swift|scala|perl|r|matlab|bash|shell|"
                            r"powershell|sql|html|css|react|angular|vue|django|flask|spring(?:\s+boot)?|dotnet|"
                            r"\.net)\b", re.IGNORECASE)

# Deliverables and subjects that belong to no business journey this platform serves.
_OFF_TOPIC = re.compile(r"\b(?:security\s+policy|privacy\s+policy|zero[\s-]?trust|threat\s+model|"
                        r"architecture(?:\s+(?:document|diagram))?|whitepaper|white\s+paper|business\s+plan|"
                        r"marketing\s+(?:copy|plan)|press\s+release|essay|poem|poetry|song|lyrics|short\s+story|"
                        r"novel|screenplay|recipe|workout|diet\s+plan|itinerary|travel\s+plan|homework|assignment|"
                        r"thesis|lesson\s+plan|blog\s+post|tutorial|legal\s+advice|medical\s+advice|"
                        r"investment\s+advice|tax\s+advice|horoscope|joke)\b|"
                        r"\b(?:secure|harden|deploy|configure|install|scale|monitor|migrate)\s+(?:the|a|an|my|our|"
                        r"your)?\s*(?:app|application|api|cluster|network|server|database|infrastructure|platform|"
                        r"system|environment|pipeline)\b|"
                        r"\b(?:kubernetes|k8s|docker|terraform|helm|aws|azure|gcp|jenkins|kafka|redis|postgres\w*|"
                        r"nginx|traefik|oauth|jwt|tls|ssl|vpn|firewall|ci/cd|devops)\b",
                        re.IGNORECASE)

# Vocabulary every industry shares: the platform's own nouns. Industry words come from the pack.
_BASE_ON_JOURNEY = ("subject", "subjects", "document", "documents", "upload", "uploaded", "attachment",
                    "screening", "screen", "assessment", "assess", "match", "matched", "matching", "score",
                    "scores", "scoring", "band", "rubric", "criteria", "criterion", "requirement", "requirements",
                    "evidence", "gap", "gaps", "grade", "graded", "report", "summary", "chat", "project",
                    "knowledge graph", "run", "workflow")


@dataclass(frozen=True)
class PolicyRuleSet:
    """The request-scope rules applied to one industry.

    on_journey:      phrases that mark a message as part of this industry's journeys; they suppress
                     `code_request` and `off_topic_request` (never `harmful_request`).
    allowed_topics:  phrases this industry may legitimately be asked about even though the base rules would call
                     them off-topic (a pack that really does answer technical questions).
    blocked_topics:  extra phrases per kind, for what an industry must never be asked.
    """

    on_journey: tuple[str, ...] = _BASE_ON_JOURNEY
    allowed_topics: tuple[str, ...] = ()
    blocked_topics: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def merge(self, other: Optional["PolicyRuleSet"]) -> "PolicyRuleSet":
        """This rule set widened by another one (base + pack). Blocked topics are the union, so a pack can add a
        rule but never remove one."""
        if other is None:
            return self
        blocked = {kind: tuple(dict.fromkeys(self.blocked_topics.get(kind, ()) + other.blocked_topics.get(kind, ())))
                   for kind in set(self.blocked_topics) | set(other.blocked_topics)}
        return PolicyRuleSet(on_journey=tuple(dict.fromkeys(self.on_journey + other.on_journey)),
                             allowed_topics=tuple(dict.fromkeys(self.allowed_topics + other.allowed_topics)),
                             blocked_topics=blocked)


BASE_RULE_SET = PolicyRuleSet()


@dataclass(frozen=True)
class PolicyReport:
    kinds: tuple[str, ...] = ()
    on_journey: bool = False        # the text uses this industry's vocabulary

    @property
    def out_of_scope(self) -> bool:
        return bool(self.kinds)


def _phrases(values: Iterable) -> tuple[str, ...]:
    """Clean, bounded, lower-cased phrases from untrusted JSON (a pack is data from the registry)."""
    cleaned = []
    for value in values or ():
        if isinstance(value, str) and value.strip():
            cleaned.append(value.strip().lower()[:MAX_PHRASE_CHARS])
    return tuple(dict.fromkeys(cleaned))[:MAX_RULE_PHRASES]


def rule_set_from_document(document: Optional[dict]) -> Optional[PolicyRuleSet]:
    """Parse a pack's `guardrails/policy.json`. Unknown keys and unknown kinds are ignored rather than raising:
    a malformed pack must degrade to the base rules, never break a check. Returns None for no usable content."""
    if not isinstance(document, dict):
        return None
    blocked_raw = document.get("blockedTopics") or document.get("blocked_topics") or {}
    blocked = {kind: _phrases(values) for kind, values in blocked_raw.items()
               if kind in POLICY_KINDS and isinstance(values, (list, tuple))} if isinstance(blocked_raw, dict) else {}
    rules = PolicyRuleSet(on_journey=_phrases(document.get("onJourney") or document.get("on_journey") or ()),
                          allowed_topics=_phrases(document.get("allowedTopics") or document.get("allowed_topics")
                                                  or ()),
                          blocked_topics={kind: values for kind, values in blocked.items() if values})
    return rules if (rules.on_journey or rules.allowed_topics or rules.blocked_topics) else None


@lru_cache(maxsize=256)
def _phrase_pattern(phrases: tuple[str, ...]) -> Optional[re.Pattern]:
    """One word-boundary alternation for a rule set's phrases (cached: rule sets are few and long-lived).
    Word boundaries matter: "policy" must not match inside "security policy" by accident of substring search."""
    if not phrases:
        return None
    return re.compile(r"\b(?:%s)\b" % "|".join(re.escape(phrase) for phrase in phrases))


def _contains(text: str, phrases: tuple[str, ...]) -> bool:
    pattern = _phrase_pattern(phrases)
    return bool(pattern and pattern.search(text))


def _kinds(text: str, rules: PolicyRuleSet) -> tuple[tuple[str, ...], bool]:
    """(kinds, on_journey) for one piece of text, already normalized and lower-cased.

    A phrase a pack lists in `blockedTopics` is unconditional - the industry has said it is never answered here, so
    on-journey vocabulary in the same message does not excuse it. The built-in heuristics below are the opposite:
    they only apply to a message that asks for something *and* uses none of the industry's vocabulary, which is
    what keeps "does this candidate know Kubernetes?" and "which clauses cover this claim?" answerable.
    """
    on_journey = _contains(text, rules.on_journey)
    asking = bool(_ASK.search(text))
    kinds: list[str] = []
    if _HARMFUL.search(text) or _contains(text, rules.blocked_topics.get(HARMFUL_REQUEST, ())):
        kinds.append(HARMFUL_REQUEST)       # never excused by on-journey vocabulary
    for kind in (CODE_REQUEST, OFF_TOPIC_REQUEST):
        if _contains(text, rules.blocked_topics.get(kind, ())):
            kinds.append(kind)
    if asking and not on_journey:
        if _CODE_ARTEFACT.search(text) or _CODE_LANGUAGE.search(text):
            kinds.append(CODE_REQUEST)
        if _OFF_TOPIC.search(text) and not _contains(text, rules.allowed_topics):
            kinds.append(OFF_TOPIC_REQUEST)
    return tuple(dict.fromkeys(kinds)), on_journey


def classify(text: str, rules: PolicyRuleSet = BASE_RULE_SET) -> PolicyReport:
    """Which scope kinds the text asks for, judged against one industry's rules. The text and every layer hidden
    inside it (base64, hex, percent-encoding - see `prompt_safety.decoded_layers`) are judged the same way, so
    "decode this: <base64 of a harmful question>" is caught by the payload, not only by the wrapper."""
    candidates = [normalize(text or "")] + decoded_layers(text or "")
    kinds: list[str] = []
    on_journey = False
    for candidate in candidates:
        found, candidate_on_journey = _kinds(candidate.lower(), rules)
        kinds.extend(found)
        on_journey = on_journey or candidate_on_journey
    return PolicyReport(tuple(dict.fromkeys(kinds)), on_journey)
