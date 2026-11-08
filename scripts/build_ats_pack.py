"""Build the ATS domain pack from the assets that live in code today.

The ATS ontology, guidance and screening prompts are currently module constants and a JSON file inside
services/ats/ats-orchestrator and services/agents/agent-skill. A domain pack wants them as *files on a skill
version* (docs/impl_multi_domain.md section 6). This script reads the real modules and writes the pack, so the
move is reproducible and nothing is transcribed by hand - rerun it whenever the sources change, until the pack
in the registry becomes the source of truth and the modules are deleted.

    uv run --directory services/common python ../../scripts/build_ats_pack.py [--out packs/ats/v1] [--check]

`--check` validates the result with agent_common.pack and prints the outcome; it writes nothing new.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "services" / "ats" / "ats-orchestrator" / "src"
AGENT_SKILL = ROOT / "services" / "agents" / "agent-skill" / "src"
DEFAULT_OUT = ROOT / "packs" / "ats" / "v1"

# Step prompt constant -> the pack path it becomes. Mirrors agent_skill.skills.registry_source.PROMPT_PATHS,
# which is what the runtime reads back.
PROMPT_FILES = {
    "JD_ANALYSIS": "prompts/jd_analysis.md",
    "PROFILE_ANALYSIS": "prompts/profile_analysis.md",
    "MATCH": "prompts/match.md",
    "VERDICT": "prompts/verdict.md",
}


def _load_module(path: Path, name: str):
    """Import a single file as a module without importing its package (which would pull in service settings)."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build(out: Path) -> dict:
    """Write the pack files and return the pack document."""
    skill_v1 = _load_module(AGENT_SKILL / "agent_skill" / "skills" / "ats_screening" / "v1.py", "_ats_v1")
    # The scoring rubric lives with the ATS screening code, not in this script: the pack must carry exactly
    # what the service would use as its packaged default, or the two silently disagree about a candidate.
    rubric_module = _load_module(AGENT_SKILL / "agent_skill" / "screening" / "rubric.py", "_ats_rubric")
    guidance = _load_module(ORCHESTRATOR / "ats_orchestrator" / "knowledge" / "guidance.py", "_ats_guidance")
    ontology_source = (ORCHESTRATOR / "ats_orchestrator" / "knowledge" / "ontologies" / "talent_v2.json")

    released = guidance.VERSIONS[guidance.DEFAULT_VERSION]
    out.mkdir(parents=True, exist_ok=True)

    written: list[str] = []

    def write(relative: str, content: str) -> str:
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        written.append(relative)
        return relative

    # The Strands skill and its four step prompts: what the runtime loads (registry_source.PROMPT_PATHS).
    write("skill.md", skill_v1.SKILL_MD)
    for constant, path in PROMPT_FILES.items():
        write(path, getattr(skill_v1, constant))

    # Knowledge-graph assets: the ontology verbatim, and the guidance the model reads with it.
    write("ontology.json", ontology_source.read_text(encoding="utf-8"))
    write("guidance/ingest.md", released.profile_extraction)
    write("guidance/answer.md", released.answering)
    write("guidance/screening_ingest.md", released.screening_extraction)

    document = {
        "id": "ats",
        "version": "v1",
        "displayName": "Recruitment (ATS)",
        "description": "Candidate screening against a job description: extract, match, grade, score, report.",
        "journeys": ["onboard_subject", "match_subject", "bulk_match", "ask_subject"],
        "subjectTypes": [{"type": "candidate", "displayName": "Candidate",
                          "schemaPath": "subjects/candidate.schema.json"}],
        "criteria": [{"key": "job_requirements", "displayName": "Job requirements",
                      "schemaPath": "criteria/job_requirements.schema.json"}],
        "rubrics": [{"key": "fit", "path": "rubrics/fit.rubric.json", "criteriaKey": "job_requirements"}],
        "ontology": {"namespace": "talent", "path": "ontology.json"},
        "guidance": {"ingestPath": "guidance/ingest.md", "answerPath": "guidance/answer.md"},
        "prompts": {name.lower(): path for name, path in PROMPT_FILES.items()},
        "reports": {},
        "tools": [
            {"server": "agent-document", "tools": ["read_document", "scan_document", "chunk_document"]},
            {"server": "agent-knowledge-graph", "tools": ["ingest_episode", "search_knowledge"]},
            {"server": "agent-guardrail", "tools": ["check_text"]},
        ],
        "execution": {"priorityTier": "standard", "maxParallelTasks": 4, "taskTimeoutSeconds": 600},
        "dataPolicy": {"classification": "confidential", "residency": "tenant-configured"},
        "humanApprovalRequiredFor": [],
    }

    # The subject payload, criteria and rubric are what the ATS screening code holds in Python today
    # (schemas.CandidateProfile / JDAnalysis, scoring.GRADE_WEIGHT). Written as pack data so insurance can
    # supply its own without a code change.
    write("subjects/candidate.schema.json", json.dumps(_candidate_schema(), indent=2))
    write("criteria/job_requirements.schema.json", json.dumps(_criteria_schema(), indent=2))
    write("rubrics/fit.rubric.json", json.dumps(rubric_module.FIT_RUBRIC, indent=2))
    write("pack.json", json.dumps(document, indent=2))
    print(f"wrote {len(written)} files to {out}")
    return document


def _candidate_schema() -> dict:
    """Minimal JSON Schema for subjects.payload of a candidate (mirrors agent_skill.schemas.CandidateProfile)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Candidate",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "name": {"type": "string"},
            "headline": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "evidence": {"type": "string"}}}},
            "roles": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "organization": {"type": "string"},
                "start_date": {"type": "string"}, "end_date": {"type": "string"}}}},
            "education": {"type": "array", "items": {"type": "object"}},
            "certifications": {"type": "array", "items": {"type": "object"}},
            "total_years": {"type": ["number", "null"]},
        },
    }


def _criteria_schema() -> dict:
    """The criteria set a candidate is graded against (mirrors agent_skill.schemas.JDAnalysis)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Job requirements",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "title": {"type": "string"},
            "sector": {"type": "string"},
            "seniority": {"type": "string"},
            "requirements": {"type": "array", "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "importance": {"type": "string", "enum": ["mandatory", "important", "nice_to_have"]},
                    "related": {"type": "array", "items": {"type": "string"}},
                    "min_years": {"type": ["number", "null"]},
                }}},
        },
    }



def check(out: Path) -> int:
    """Validate the generated pack exactly as registry-service does on publish."""
    from agent_common.errors import AppError
    from agent_common.pack import PACK_FILE, parse_pack, validate_pack

    pack_path = out / PACK_FILE
    if not pack_path.exists():
        print(f"no {PACK_FILE} in {out}; run without --check first")
        return 1
    available = {str(path.relative_to(out)).replace("\\", "/") for path in out.rglob("*") if path.is_file()}
    try:
        document = parse_pack(pack_path.read_text(encoding="utf-8"))
        validate_pack(document, available=available)
    except AppError as error:
        print(f"INVALID: {error.spec.code} {error.spec.message}")
        for field in error.fields or []:
            print(f"  - {field.get('field')}: {field.get('message')}")
        return 1
    print(f"VALID: pack {document.id}@{document.version} - {len(available)} files, "
          f"{len(document.referenced_paths())} references, journeys: {', '.join(document.journeys)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="validate an already-generated pack and exit")
    args = parser.parse_args()
    if args.check:
        return check(args.out)
    build(args.out)
    return check(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
