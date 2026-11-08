"""Domain packs: the declarative bundle that makes one platform serve many domains.

A pack supplies a domain's vocabulary, schemas, prompts, rubrics, ontology and tool grants; the capabilities
(document, knowledge graph, match, rules, sandbox) stay shared and domain-neutral. See
docs/impl_multi_domain.md for why this boundary, and section 6 there for the file layout.

Usage - the registry calls this before a version may be written or published:

    from agent_common.pack import PACK_FILE, parse_pack, validate_pack

    document = parse_pack(files["pack.json"])          # raises AppError(PCK-INV-0001) on a bad document
    validate_pack(document, available=set(files))      # raises AppError(PCK-INV-0002) on a dangling reference

Nothing here executes pack content; a pack is data (and `PackFileRef` refuses code file types outright).
"""
import json
from typing import Union

from agent_common.errors import AppError
from agent_common.pack.errors import PackErrors
from agent_common.pack.models import (CLASSIFICATIONS, JOURNEYS, CriteriaSpec, DataPolicy, ExecutionProfile,
                                      GuidanceSpec, OntologySpec, PackDocument, PackFileRef, RubricSpec,
                                      SubjectTypeSpec, ToolGrant)

PACK_FILE = "pack.json"          # a version carrying this file is a pack and is validated as one


def parse_pack(raw: Union[str, bytes, dict, None]) -> PackDocument:
    """Parse and validate a pack document.

    Accepts the file's text or an already-decoded dict. Raises AppError(PCK-INV-0001) for anything malformed -
    the underlying pydantic/JSON message is attached as a field hint, never a stack trace.
    """
    if raw is None or (isinstance(raw, (str, bytes)) and not str(raw).strip()):
        raise AppError(PackErrors.PACK_INVALID, fields=[{"field": PACK_FILE, "message": "the pack file is empty"}])
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError) as error:
        raise AppError(PackErrors.PACK_INVALID,
                       fields=[{"field": PACK_FILE, "message": f"not valid JSON: {error}"}]) from None
    if not isinstance(data, dict):
        raise AppError(PackErrors.PACK_INVALID,
                       fields=[{"field": PACK_FILE, "message": "the pack file must be a JSON object"}])
    try:
        return PackDocument.model_validate(data)
    except Exception as error:                      # pydantic ValidationError and anything a validator raises
        raise AppError(PackErrors.PACK_INVALID,
                       fields=[{"field": PACK_FILE, "message": _first_problem(error)}]) from None


def validate_pack(document: PackDocument, available: set[str]) -> None:
    """Check every reference resolves against the version's files.

    `available` is the set of paths the version actually contains. A pack that points at a file nobody uploaded
    would fail at run time, on a user's request - so it is refused at authoring time instead.
    """
    problems: list[dict] = []
    for path in document.missing_paths(available):
        problems.append({"field": path, "message": "referenced by pack.json but not present in this version"})
    for key in document.dangling_rubric_criteria():
        problems.append({"field": f"rubrics.criteria_key={key}",
                         "message": "names a criteria key this pack does not define"})
    if problems:
        raise AppError(PackErrors.PACK_REFERENCES_INVALID, fields=problems)


def _first_problem(error: Exception) -> str:
    """The first validation problem as one short, safe sentence (never a stack trace)."""
    errors = getattr(error, "errors", None)
    if callable(errors):
        for item in errors():
            location = ".".join(str(part) for part in item.get("loc", ())) or PACK_FILE
            return f"{location}: {item.get('msg', 'invalid value')}"
    return str(error)[:200]


__all__ = ["CLASSIFICATIONS", "JOURNEYS", "PACK_FILE", "CriteriaSpec", "DataPolicy", "ExecutionProfile",
           "GuidanceSpec", "OntologySpec", "PackDocument", "PackErrors", "PackFileRef", "RubricSpec",
           "SubjectTypeSpec", "ToolGrant", "parse_pack", "validate_pack"]
