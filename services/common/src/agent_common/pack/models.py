"""Data model for a domain pack (docs/impl_multi_domain.md section 6).

A pack is what makes one platform serve ATS, insurance, banking and media without a code fork: it supplies the
vocabulary, schemas, prompts, rubrics and tool grants of a domain, while the capabilities stay shared. Packs are
*data*, exactly like access policies: the document is validated on publish, stored as files on a skill version,
and pinned per run.

Two rules shape this model, and both are security controls rather than style:

* **Nothing in a pack is executable.** A pack carries prompts, schemas, rubrics and templates. Anything that
  needs real code goes to the sandbox as an approved, scanned, versioned script. `PackFileRef` therefore
  refuses code file extensions outright.
* **Every reference must resolve.** A path named here must exist among the version's files, and a tool grant
  must name its tools explicitly - no wildcard that quietly widens as a server gains tools. `missing_paths`
  and the validators below are what the registry calls before a version may be published.

The grade vocabulary lives in the rubric, not in Python: insurance will want
"covered / excluded / conditional / insufficient_evidence" where ATS wants "strong / moderate / ...", and that
must be a pack edit, not a code change.
"""
import posixpath
import re
from typing import Optional

from pydantic import Field, field_validator

from agent_common.models import ApiModel

# A pack id is the domain key the rest of the platform already uses (Skill.domain, subjects.domain).
PACK_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
PACK_VERSION = re.compile(r"^v?\d{1,4}(\.\d{1,4})?$")
KEY_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SUBJECT_TYPE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# The journeys of docs/impl_multi_domain.md section 4. A pack declares which ones it offers; the planner will
# not build a plan for a journey a pack has not enabled.
JOURNEYS = frozenset({"onboard_subject", "match_subject", "bulk_match", "ask_subject", "ask_across", "review"})

# Data classifications, lowest to highest sensitivity. Used for routing, retention and log redaction decisions.
CLASSIFICATIONS = frozenset({"public", "internal", "confidential", "restricted", "special_category"})

# A pack file is content, never code: these extensions are refused so a pack can never smuggle in something
# that a runtime might be tempted to execute or import.
EXECUTABLE_SUFFIXES = frozenset({".py", ".pyc", ".pyo", ".so", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
                                 ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".jar", ".exe", ".dll",
                                 ".dylib", ".wasm"})
ALLOWED_SUFFIXES = frozenset({".json", ".md", ".txt", ".yaml", ".yml", ".j2", ".jinja", ".jinja2", ".csv"})

MAX_PATH_LENGTH = 255


def _check_path(value: str) -> str:
    """A pack path is relative, normalised, inside the version, and not executable.

    Rejects absolute paths, `..` traversal, backslashes, and any code extension. The registry stores files by
    path, so a traversal here would let a pack address something outside its own version.
    """
    path = value.strip()
    if not path or len(path) > MAX_PATH_LENGTH:
        raise ValueError(f"path must be 1-{MAX_PATH_LENGTH} characters")
    if "\\" in path:
        raise ValueError("path must use forward slashes")
    if path.startswith("/") or path.startswith("~"):
        raise ValueError("path must be relative to the pack root")
    if posixpath.normpath(path) != path or path.startswith("../") or "/../" in path:
        raise ValueError("path must be normalised and must not traverse outside the pack")
    suffix = posixpath.splitext(path)[1].lower()
    if suffix in EXECUTABLE_SUFFIXES:
        raise ValueError(f"a pack carries content, not code: '{suffix}' files are not allowed")
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(f"unsupported pack file type '{suffix}' (allowed: {', '.join(sorted(ALLOWED_SUFFIXES))})")
    return path


class PackFileRef(ApiModel):
    """A pointer to one file of the version that carries this pack."""

    path: str

    _validate_path = field_validator("path")(lambda cls, v: _check_path(v))


class SubjectTypeSpec(ApiModel):
    """A kind of record this pack's journeys are about, and the schema its `subjects.payload` must satisfy."""

    type: str = Field(pattern=SUBJECT_TYPE.pattern)
    schema_path: str
    display_name: str = Field(default="", max_length=128)

    _validate_path = field_validator("schema_path")(lambda cls, v: _check_path(v))


class CriteriaSpec(ApiModel):
    """A set of criteria a subject is graded against (JD requirements, coverage clauses, KYC controls)."""

    key: str = Field(pattern=KEY_NAME.pattern)
    schema_path: str
    display_name: str = Field(default="", max_length=128)

    _validate_path = field_validator("schema_path")(lambda cls, v: _check_path(v))


class RubricSpec(ApiModel):
    """A deterministic scoring rubric: grade vocabulary, weights, thresholds, bands.

    Read by the rules capability, never by an LLM - no model computes a score (CLAUDE.md section 2).
    """

    key: str = Field(pattern=KEY_NAME.pattern)
    path: str
    criteria_key: Optional[str] = Field(default=None, pattern=KEY_NAME.pattern)

    _validate_path = field_validator("path")(lambda cls, v: _check_path(v))


class OntologySpec(ApiModel):
    """The pack's knowledge-graph ontology and the namespace its graphs live in.

    `namespace` becomes part of every graph id, so two packs never share a graph.
    """

    namespace: str = Field(pattern=KEY_NAME.pattern)
    path: str

    _validate_path = field_validator("path")(lambda cls, v: _check_path(v))


class GuidanceSpec(ApiModel):
    """Extraction and answering guidance: the brief the model reads, versioned with the pack."""

    ingest_path: Optional[str] = None
    answer_path: Optional[str] = None

    @field_validator("ingest_path", "answer_path")
    @classmethod
    def _validate_optional_path(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _check_path(value)


class ToolGrant(ApiModel):
    """Which tools of which MCP service this pack's journeys may call.

    Explicit names only: a wildcard would silently widen the grant every time that server gained a tool, which
    is the opposite of the default-deny the rest of the platform enforces.
    """

    server: str = Field(pattern=SERVICE_NAME.pattern)
    tools: list[str] = Field(min_length=1, max_length=100)

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, values: list[str]) -> list[str]:
        for tool in values:
            if tool == "*" or "*" in tool:
                raise ValueError("tool grants must name tools explicitly; wildcards are not allowed")
            if not TOOL_NAME.match(tool):
                raise ValueError(f"invalid tool name '{tool}'")
        if len(set(values)) != len(values):
            raise ValueError("duplicate tool in grant")
        return values


class ExecutionProfile(ApiModel):
    """How this pack's runs behave: priority class, parallelism and timeouts.

    `priority_tier` is a *request* for a tier; the server still derives the effective tier from the tenant's
    plan (docs/impl_domain_platform.md Stage F). A pack cannot promote itself.
    """

    priority_tier: str = Field(default="standard", pattern=r"^(premium|standard|batch)$")
    max_parallel_tasks: int = Field(default=4, ge=1, le=64)
    task_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    run_timeout_seconds: int = Field(default=3_600, ge=1, le=604_800)


class DataPolicy(ApiModel):
    """Classification and retention defaults for everything this pack produces."""

    classification: str = Field(default="confidential")
    residency: str = Field(default="tenant-configured", max_length=64)
    retention_days: Optional[int] = Field(default=None, ge=1, le=36_500)

    @field_validator("classification")
    @classmethod
    def _validate_classification(cls, value: str) -> str:
        if value not in CLASSIFICATIONS:
            raise ValueError(f"classification must be one of {', '.join(sorted(CLASSIFICATIONS))}")
        return value


class PackDocument(ApiModel):
    """One domain pack, as stored in a version's `pack.json`.

    Validated whenever a version carrying a pack.json is written or published; an invalid pack never activates,
    so a broken pack cannot reach a run.
    """

    id: str = Field(pattern=PACK_ID.pattern)
    version: str = Field(pattern=PACK_VERSION.pattern)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)
    journeys: list[str] = Field(min_length=1, max_length=len(JOURNEYS))
    subject_types: list[SubjectTypeSpec] = Field(default_factory=list, max_length=50)
    criteria: list[CriteriaSpec] = Field(default_factory=list, max_length=50)
    rubrics: list[RubricSpec] = Field(default_factory=list, max_length=50)
    ontology: Optional[OntologySpec] = None
    guidance: GuidanceSpec = Field(default_factory=GuidanceSpec)
    prompts: dict[str, str] = Field(default_factory=dict)
    reports: dict[str, str] = Field(default_factory=dict)
    tools: list[ToolGrant] = Field(default_factory=list, max_length=50)
    execution: ExecutionProfile = Field(default_factory=ExecutionProfile)
    data_policy: DataPolicy = Field(default_factory=DataPolicy)
    human_approval_required_for: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("journeys")
    @classmethod
    def _validate_journeys(cls, values: list[str]) -> list[str]:
        unknown = [journey for journey in values if journey not in JOURNEYS]
        if unknown:
            raise ValueError(f"unknown journey(s) {', '.join(unknown)}; known: {', '.join(sorted(JOURNEYS))}")
        if len(set(values)) != len(values):
            raise ValueError("duplicate journey")
        return values

    @field_validator("prompts", "reports")
    @classmethod
    def _validate_path_map(cls, values: dict[str, str]) -> dict[str, str]:
        if len(values) > 100:
            raise ValueError("at most 100 entries")
        for key, path in values.items():
            if not KEY_NAME.match(key):
                raise ValueError(f"invalid key '{key}'")
            _check_path(path)
        return values

    @field_validator("subject_types")
    @classmethod
    def _unique_subject_types(cls, values: list[SubjectTypeSpec]) -> list[SubjectTypeSpec]:
        if len({spec.type for spec in values}) != len(values):
            raise ValueError("duplicate subject type")
        return values

    @field_validator("criteria")
    @classmethod
    def _unique_criteria(cls, values: list[CriteriaSpec]) -> list[CriteriaSpec]:
        if len({spec.key for spec in values}) != len(values):
            raise ValueError("duplicate criteria key")
        return values

    @field_validator("rubrics")
    @classmethod
    def _unique_rubrics(cls, values: list[RubricSpec]) -> list[RubricSpec]:
        if len({spec.key for spec in values}) != len(values):
            raise ValueError("duplicate rubric key")
        return values

    @field_validator("tools")
    @classmethod
    def _unique_servers(cls, values: list[ToolGrant]) -> list[ToolGrant]:
        if len({grant.server for grant in values}) != len(values):
            raise ValueError("duplicate tool grant server")
        return values

    def referenced_paths(self) -> list[str]:
        """Every file path this pack points at, so the caller can check they all exist in the version."""
        paths: list[str] = []
        paths.extend(spec.schema_path for spec in self.subject_types)
        paths.extend(spec.schema_path for spec in self.criteria)
        paths.extend(spec.path for spec in self.rubrics)
        if self.ontology is not None:
            paths.append(self.ontology.path)
        paths.extend(path for path in (self.guidance.ingest_path, self.guidance.answer_path) if path)
        paths.extend(self.prompts.values())
        paths.extend(self.reports.values())
        return paths

    def missing_paths(self, available: set[str]) -> list[str]:
        """Referenced paths that are not among the version's files - a pack with any of these cannot publish."""
        return sorted({path for path in self.referenced_paths() if path not in available})

    def dangling_rubric_criteria(self) -> list[str]:
        """Rubrics naming a criteria key the pack does not define (a rubric that can never be applied)."""
        known = {spec.key for spec in self.criteria}
        return sorted({spec.criteria_key for spec in self.rubrics
                       if spec.criteria_key is not None and spec.criteria_key not in known})
