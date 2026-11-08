"""Text predicates for grounding: is this term, phrase or name actually written in the source?

Every function here is pure, deterministic and domain-free. They exist because grounding must not be a
judgement call: "the model said the candidate knows Kubernetes" is worth nothing unless the word is in the
resume, and the same is true of a coverage clause in a policy or a control in an audit file.

The matching is deliberately conservative. It is better to drop a claim the source does support than to keep
one it does not, because a kept claim becomes a score, and a score becomes a decision about a person.
"""
import re

# Words that carry no evidential weight on their own. They are the language of *requirements* rather than of
# any one industry ("proven", "ability", "mandatory"), so they are as useless in a policy wording as in a job
# description. A caller with its own noise words passes them in rather than editing this set.
DEFAULT_STOPWORDS = frozenset({
    "a", "an", "and", "or", "of", "the", "to", "in", "on", "for", "with", "experience", "knowledge", "strong",
    "good", "proven", "ability", "skills", "skill", "years", "year", "must", "have", "nice", "plus", "required",
    "preferred", "understanding", "familiarity", "hands", "working", "mandatory", "compulsory", "compulsary",
    "important", "also",
})

# Enough of a key-word overlap to treat a name as named by the source even when it is not written verbatim.
DEFAULT_OVERLAP = 0.6

_WORD = re.compile(r"[a-z0-9.#+][a-z0-9+#./-]*")


def normalize(text: str) -> str:
    """Lower-case, with smart quotes flattened and runs of whitespace collapsed."""
    text = text.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def key_words(text: str, stopwords: frozenset[str] = DEFAULT_STOPWORDS) -> set[str]:
    """The words of `text` that carry meaning, normalised and stripped of punctuation."""
    words = _WORD.findall(normalize(text))
    return {word.strip("./-") for word in words
            if word.strip("./-") not in stopwords and len(word.strip("./-")) > 1}


def term_in_text(term: str, text: str) -> bool:
    """Whole-term, case-insensitive match that survives names like '.NET', 'C++', 'C#' and 'Node.js'.

    A plain substring search would match 'Java' inside 'JavaScript' and award credit for a skill nobody
    claimed, so the boundaries are explicit about the characters that may legitimately end such a name.
    """
    needle = normalize(term)
    if len(needle) < 2:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9+#])", normalize(text)) is not None


def phrase_in_text(phrase: str, text: str) -> bool:
    """Is this phrase written in the source, word for word (ignoring case and spacing)?"""
    return bool(phrase.strip()) and normalize(phrase) in normalize(text)


def snippet(term: str, text: str, width: int = 60) -> str:
    """A short quotation of the source around `term`, or '' when it is not there.

    This is what a person reads to check a grade, so it is taken from the stored source rather than from
    anything a model wrote.
    """
    flat = normalize(text)
    match = re.search(re.escape(normalize(term)), flat)
    return flat[max(0, match.start() - width): match.end() + width].strip() if match else ""


def same(left: str, right: str) -> bool:
    """Equal once normalised - the comparison used whenever two names must mean one thing."""
    return normalize(left) == normalize(right)


def supported_by(name: str, quote: str, text: str, *, stopwords: frozenset[str] = DEFAULT_STOPWORDS,
                 overlap: float = DEFAULT_OVERLAP) -> bool:
    """Is a named item really in this source: quoted verbatim, named outright, or mostly worded in it?

    The third test is what keeps rewording honest without letting invention through. A model that reports
    "Kubernetes cluster administration" for a source saying "administering Kubernetes clusters" is being
    accurate; one that reports a term sharing no words with the source is not.
    """
    if phrase_in_text(quote, text) or term_in_text(name, text):
        return True
    words = key_words(name, stopwords)
    return bool(words) and len(words & key_words(text, stopwords)) / len(words) >= overlap
