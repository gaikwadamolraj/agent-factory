"""Data model for the IAM-style access policy engine (agent_common.policy).

Policies are *data* (stored as JSONB in `access_policies`, edited from the admin UI); the engine is code. A policy
document is a list of statements; each statement allows or denies a set of actions on a set of resources, optionally
guarded by a small, fixed set of conditions. Actions and resources are namespaced, glob-able strings:

    actions    skill.run, skill.read, skill.approve, tool:agent-compute.run, admin.users.manage, policy.manage
    resources  skill:ats/screening, skill:ats/*, skill:*:draft, client:123, project:*

Wildcards: `*` matches any run of characters (see agent_common.policy.matcher). Evaluation semantics live in
agent_common.policy.evaluator (explicit deny wins, otherwise an allow must match, else default deny).
"""
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Effect(StrEnum):
    """Whether a matching statement grants or refuses access."""

    ALLOW = "allow"
    DENY = "deny"


class Statement(BaseModel):
    """One rule: an effect over actions x resources, optionally narrowed by conditions.

    A statement *matches* an access request when the action matches one of `actions`, the resource matches one of
    `resources`, and every condition in `conditions` holds (see agent_common.policy.conditions). `conditions` keys are
    condition names from the fixed registry; values are the booleans that enable them (e.g. {"unless_author": true}).
    """

    model_config = ConfigDict(extra="forbid")

    effect: Effect
    actions: list[str] = Field(min_length=1)
    resources: list[str] = Field(min_length=1)
    conditions: dict[str, bool] = Field(default_factory=dict)


class PolicyDocument(BaseModel):
    """A set of statements, as stored in one `access_policies.document` row."""

    model_config = ConfigDict(extra="forbid")

    statements: list[Statement] = Field(default_factory=list)


class AccessRequest(BaseModel):
    """A single 'can subject do action on resource?' question handed to the evaluator.

    `resource_attributes` carries the facts the conditions need about the resource (its owning client, its project,
    the author of a draft). They are looked up from stored data by the caller - never taken from the LLM - so a
    condition compares verified values, not a string parsed out of the resource name.
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    resource: str
    resource_client_id: Optional[int] = None    # which client owns the resource (for same_client)
    resource_project_id: Optional[int] = None   # which project the resource belongs to (for project_member)
    resource_author_id: Optional[int] = None     # the author of a draft resource (for draft_author / unless_author)


class Subject(BaseModel):
    """Who is asking, with the memberships that bind extra policies to them.

    The subject's own policies plus the policies of its groups, projects and client make up its *effective* policies
    (see agent_common.policy.loader); the memberships here are also what the conditions compare against.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    client_id: int
    group_ids: frozenset[int] = Field(default_factory=frozenset)
    project_ids: frozenset[int] = Field(default_factory=frozenset)
    is_super_admin: bool = False


class PolicyDecision(BaseModel):
    """The outcome of evaluating one AccessRequest, with the statement that decided it (for the UI 'simulate' view).

    `allowed` is the answer. `effect` is "allow", "deny" (an explicit deny matched) or "default" (nothing matched, so
    default-deny). When a statement decided, `policy_index`/`statement_index` point at it and `matched_conditions`
    lists the conditions that had to hold - enough to explain the decision in the admin UI.
    """

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    effect: str
    reason: str
    policy_index: Optional[int] = None
    statement_index: Optional[int] = None
    matched_conditions: list[str] = Field(default_factory=list)
