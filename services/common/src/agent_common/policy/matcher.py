"""The wildcard matcher for policy actions and resources. Deliberately tiny and documented, so what a policy grants
is always obvious.

Rules:
* An exact string matches itself: `skill.run` matches `skill.run`.
* `*` is the only wildcard. It matches any run of characters, including the `.`, `:` and `/` separators. So:
    - `*`              matches everything (use sparingly: a super-admin grant);
    - `skill.*`        matches `skill.run`, `skill.read`, `skill.approve`;
    - `skill:ats/*`    matches `skill:ats/screening` and `skill:ats/anything/deeper`;
    - `skill:*:draft`  matches `skill:ats/screening:draft` (the `*` spans the middle).
* `*` may appear several times in a pattern; every other character is literal (no `?`, no character classes), so a
  dot or colon in a pattern only ever matches that same character.
"""
import re
from functools import lru_cache


@lru_cache(maxsize=4096)
def _compiled(pattern: str) -> re.Pattern[str]:
    """Translate a policy pattern into an anchored regex, treating only `*` as a wildcard (everything else literal)."""
    # Split on '*' so each literal chunk is regex-escaped and '*' becomes '.*' (matches any characters, separators too)
    parts = pattern.split("*")
    regex = ".*".join(re.escape(part) for part in parts)
    return re.compile(f"^{regex}$", re.DOTALL)


def matches(pattern: str, value: str) -> bool:
    """True when `value` matches the policy `pattern` (see the module docstring for the rules)."""
    if pattern == value:              # fast path for the common exact match
        return True
    return _compiled(pattern).fullmatch(value) is not None


def matches_any(patterns: list[str], value: str) -> bool:
    """True when `value` matches at least one of the patterns."""
    return any(matches(pattern, value) for pattern in patterns)
