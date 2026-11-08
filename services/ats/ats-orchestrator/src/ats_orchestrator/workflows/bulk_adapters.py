"""The ATS grader: what "grade this subject against these criteria" means for recruitment.

`bulk_activities.py` is domain-neutral - it selects subjects, asks for each to be graded, records the result
and ranks. This file is the only part that knows the criteria set is a job description, the subject is a
candidate, and grading is a screening. Insurance supplies an equivalent that reads a policy and a claim, and
nothing in the workflow or the activities changes (docs/impl_multi_domain.md section 2.2).

Everything is tenant-scoped through `TenantScope`, so a worker grading one client's queue entry can never
read another's documents (CLAUDE.md section 7).
"""
from typing import Optional

from agent_common.db import Database, SubjectRepository
from agent_common.db.repositories import TenantScope
from agent_common.logging import get_logger, log_event

from ats_orchestrator.config import OrchestratorSettings
from ats_orchestrator.repositories import DocumentRepository
from ats_orchestrator.services.agent_client import AgentClient, AgentTenant

log = get_logger(__name__)

# The criteria reference the producer passes is the same `document:<id>` form the knowledge graph uses as a
# subject ref, so one vocabulary covers "which document" everywhere in the platform.
DOCUMENT_PREFIX = "document:"


def document_id_of(reference: str) -> Optional[int]:
    """The document id inside a `document:<id>` reference, or None when it is not one."""
    if not reference.startswith(DOCUMENT_PREFIX):
        return None
    try:
        return int(reference[len(DOCUMENT_PREFIX):])
    except ValueError:
        return None


class CandidateGrader:
    """Grades one candidate against one job description, through the screening agent.

    The bulk path reuses the *same* agent and the same skill version as an interactive screening on purpose.
    A shortlist that a recruiter cannot reproduce by screening one candidate by hand would be worthless, and
    two scoring paths would drift the moment either was changed.
    """

    def __init__(self, database: Database, settings: OrchestratorSettings, agent: AgentClient) -> None:
        self._database, self._settings, self._agent = database, settings, agent

    async def grade(self, *, client_id: int, project_id: int, subject_id: int, criteria_ref: str,
                    guidance_version: str = "") -> dict:
        """Score one candidate. Returns the small, storable shape `match_results` needs.

        Raises when the resume or the job description cannot be read, so the caller records the failure
        against that subject rather than publishing a zero that looks like a judgement.
        """
        scope = TenantScope(client_id, project_id)
        jd_document_id = document_id_of(criteria_ref)
        if jd_document_id is None:
            raise ValueError(f"criteria reference {criteria_ref!r} is not a document reference")

        async with self._database.session() as session:
            documents = DocumentRepository(session, scope)
            jd = await documents.get_scoped(jd_document_id)
            subject = await SubjectRepository(session).get(subject_id)
            resume_document_id = int((subject.payload or {}).get("documentId", 0)) if subject else 0
            resume = await documents.get_scoped(resume_document_id) if resume_document_id else None
            jd_text = (jd.text or "") if jd is not None else ""
            resume_text = (resume.text or "") if resume is not None else ""

        if not jd_text.strip():
            raise ValueError(f"job description {jd_document_id} has no readable text in this project")
        if not resume_text.strip():
            raise ValueError(f"subject {subject_id} has no readable resume in this project")

        limit = self._settings.knowledge_max_text_chars
        tenant = AgentTenant(client_id=client_id, project_id=project_id, user_id=0)
        # `chat_id` identifies the agent's working session. A bulk run is not a chat, so it gets its own id
        # per subject: the screening must not inherit or leave behind conversational memory that would make
        # one candidate's result depend on the candidate graded before them.
        result = await self._agent.screen(tenant, request_id=f"bulk-{subject_id}",
                                          chat_id=f"bulk:{criteria_ref}:{subject_id}",
                                          job_description=jd_text[:limit], resume_text=resume_text[:limit])
        log_event(log, "bulk.candidate_scored", subject_id=subject_id, score=result.get("score"))
        return _as_match_result(result)


def _as_match_result(screening: dict) -> dict:
    """Map a ScreeningResult onto the columns `match_results` stores.

    Only what explains the score is kept: the grades per requirement and the rubric's component points. The
    narrative summary and the interview questions belong to an interactive screening, not to a row in a
    shortlist of a thousand.
    """
    grades = {str(requirement.get("name", ""))[:200]: str(requirement.get("grade", ""))
              for requirement in screening.get("requirements", []) if requirement.get("name")}
    return {"score": screening.get("score"), "band": screening.get("band", ""),
            "recommendation": screening.get("recommendation", ""),
            "rubricKey": screening.get("rubricKey", "fit"),
            "rubricVersion": screening.get("rubricVersion", ""),
            "grades": grades, "breakdown": dict(screening.get("breakdown", {}) or {})}


def build_bulk_dependencies(database: Database, settings: OrchestratorSettings, agent: AgentClient) -> dict:
    """The keyword arguments `BulkMatchActivities` takes, built from the container's objects."""
    return {"grader": CandidateGrader(database, settings, agent)}
