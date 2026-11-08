"""The fixed, documented set of policy conditions. Conditions are NOT arbitrary code: each one is a named, typed
handler in a registry, so a statement can only use conditions that exist here, and adding a new condition is exactly
one function plus one registry entry.

A condition handler answers "does this condition hold for this (subject, request)?". A statement matches only when
every condition it names holds (logical AND). A condition's value in the policy document is the boolean that *enables*
it; we only support enabled (`true`) conditions, so a value of `false` disables the condition (it is ignored).

Current conditions:
* same_client      - the resource's owning client equals the subject's client.
* project_member   - the subject is a member of the resource's project.
* draft_author     - the subject is the author of the (draft) resource.
* unless_author    - the subject is NOT the author (used on a deny, e.g. "deny running a draft unless you wrote it").
"""
from collections.abc import Callable

from agent_common.policy.models import AccessRequest, Subject

# A handler decides whether its condition holds for this subject and request.
ConditionHandler = Callable[[Subject, AccessRequest], bool]


def _same_client(subject: Subject, request: AccessRequest) -> bool:
    """The resource belongs to the subject's own client (tenant isolation at the policy layer)."""
    return request.resource_client_id is not None and request.resource_client_id == subject.client_id


def _project_member(subject: Subject, request: AccessRequest) -> bool:
    """The subject is a member of the project the resource belongs to."""
    return request.resource_project_id is not None and request.resource_project_id in subject.project_ids


def _draft_author(subject: Subject, request: AccessRequest) -> bool:
    """The subject authored the resource (e.g. may act on their own draft version)."""
    return request.resource_author_id is not None and request.resource_author_id == subject.user_id


def _unless_author(subject: Subject, request: AccessRequest) -> bool:
    """The subject is NOT the resource's author. On a deny statement this makes the deny apply to everyone except
    the author (the inverse of draft_author)."""
    return request.resource_author_id is None or request.resource_author_id != subject.user_id


# The whole condition vocabulary. Add a condition by writing a handler and registering it here - nothing else.
CONDITION_HANDLERS: dict[str, ConditionHandler] = {
    "same_client": _same_client,
    "project_member": _project_member,
    "draft_author": _draft_author,
    "unless_author": _unless_author,
}


def conditions_hold(conditions: dict[str, bool], subject: Subject, request: AccessRequest) -> bool:
    """True when every *enabled* condition on a statement holds for this subject and request.

    A condition set to `false` is disabled and skipped. An unknown condition name is treated as NOT holding, so a
    statement that names a condition the engine does not understand never grants access (fail-closed).
    """
    for name, enabled in conditions.items():
        if not enabled:                       # value false disables the condition
            continue
        handler = CONDITION_HANDLERS.get(name)
        if handler is None or not handler(subject, request):
            return False
    return True


def enabled_condition_names(conditions: dict[str, bool]) -> list[str]:
    """The names of the conditions that are switched on (for explaining a decision in the admin UI)."""
    return [name for name, enabled in conditions.items() if enabled]
