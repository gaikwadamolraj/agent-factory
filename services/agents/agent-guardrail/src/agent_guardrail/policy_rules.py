"""Where an industry's request-scope rules come from.

A rule set says which vocabulary is on-journey for one industry and which topics it must never be asked about
(`agent_common.content_policy`). It is **data carried by the domain pack**, so recruitment and insurance block
different things without a code change, and a new industry is a new pack rather than a new release.

`PackDirectoryRuleSets` reads the packs from a directory (`<packs_dir>/<pack>/<version>/guardrails/policy.json`,
highest version wins). The interface is one method, `for_pack(pack_id)`, so serving the same file from
registry-service later - the client's *activated* pack version, cached by the skill-version counter, exactly like
`agent_common.pack.source` does for guidance - is a new class here and no change anywhere else.

Zero trust: the pack id arrives in a request, so it is validated against `PACK_ID` before it is ever used as a path
segment. Any failure (missing pack, unreadable or malformed file) falls back to the base rules and is logged:
request-scope rules must never be the reason a check fails.
"""
import json
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from agent_common.content_policy import BASE_RULE_SET, PolicyRuleSet, rule_set_from_document
from agent_common.logging import get_logger, log_event
from agent_common.pack.models import PACK_ID

log = get_logger(__name__)
POLICY_FILE = "guardrails/policy.json"
MAX_POLICY_BYTES = 256_000


class PackDirectoryRuleSets:
    """Industry rule sets read from a packs directory, cached (bounded, least-recently-used)."""

    def __init__(self, packs_dir: str, max_entries: int = 32) -> None:
        self._root = Path(packs_dir) if packs_dir else None
        self._max_entries = max(1, max_entries)
        self._cache: "OrderedDict[str, PolicyRuleSet]" = OrderedDict()

    def for_pack(self, pack: str) -> PolicyRuleSet:
        """The base rules merged with this industry's rules; the base rules alone when the pack has none."""
        key = (pack or "").strip().lower()
        if not key or self._root is None or not PACK_ID.match(key):
            return BASE_RULE_SET
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        rules = BASE_RULE_SET.merge(self._load(key))
        self._cache[key] = rules
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return rules

    def _load(self, pack: str) -> Optional[PolicyRuleSet]:
        """Parse the pack's policy file, or None when it is absent, too large or malformed (always logged)."""
        path = self._policy_path(pack)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAX_POLICY_BYTES:
                log_event(log, "guardrails.policy_too_large", pack=pack)
                return None
            rules = rule_set_from_document(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            log_event(log, "guardrails.policy_unreadable", pack=pack, error_type=type(error).__name__)
            return None
        log_event(log, "guardrails.policy_loaded", pack=pack,
                  on_journey=len(rules.on_journey) if rules else 0,
                  blocked=sum(len(values) for values in rules.blocked_topics.values()) if rules else 0)
        return rules

    def _policy_path(self, pack: str) -> Optional[Path]:
        """The policy file of the pack's highest version directory, or None when there is none."""
        pack_root = self._root / pack
        if not pack_root.is_dir():
            return None
        versions = sorted((child for child in pack_root.iterdir() if child.is_dir()), key=lambda child: child.name)
        for version in reversed(versions):
            candidate = version / POLICY_FILE
            if candidate.is_file():
                return candidate
        return None
