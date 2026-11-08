"""Exact answers for calculation questions (dates, durations, counts, sums) over the chat's data.

The code author (one LLM call, answering with a ```python block; models write far better code as plain text than
inside a JSON string) writes a Python program from the question and the data; the compute agent (agent-compute) runs
it in the sandbox and returns the output as text. If the run fails, the author gets the error text and writes a fixed
program. Every author call and every run spends one step of the request's StepBudget, and there are at most
`max_code_attempts` attempts, so this always ends.

Quality: the hard, error-prone part (parsing free-form dates) is done here in plain code. Every program is prefixed
with a PRELUDE that defines TODAY, ROLES (with real `date` objects) and duration helpers, so the model only writes
the question-specific logic.
"""
import json
import re
import uuid
from datetime import date
from typing import Optional

from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from ats_orchestrator.assistant.graph import build_llm, invoke_llm
from ats_orchestrator.config import OrchestratorSettings
from ats_orchestrator.services.agent_client import ComputeClient
from ats_orchestrator.services.step_budget import StepBudget

log = get_logger(__name__)
_EXIT_CODE = re.compile(r"^exit code: (-?\d+)")
_CODE_BLOCK = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_ONGOING = {"present", "current", "now", "till date", "to date", "ongoing", "today"}

PRELUDE = '''import json
from datetime import date

TODAY = date.fromisoformat("{today}")


def months_between(start: date, end: date) -> int:
    \"\"\"Whole months from start to end (negative if end is before start).\"\"\"
    return (end.year - start.year) * 12 + (end.month - start.month)


def years_months(months: int) -> str:
    years, rest = divmod(abs(months), 12)
    return f"{{years}} years {{rest}} months"


def _month(value):
    return date.fromisoformat(value + "-01") if value else None


def role_label(role) -> str:
    return f"{{role['title'] or 'Role'}} at {{role['organization'] or 'unknown organization'}}"


# One dict per role: title, organization, start (date or None), end (date; TODAY when ongoing), ongoing (bool),
# start_text / end_text (as written in the resume), month_known (False when only the year was given), months.
ROLES = [dict(r, start=_month(r["start"]), end=_month(r["end"]) or TODAY) for r in json.loads({roles_json})]
for _r in ROLES:
    _r["months"] = months_between(_r["start"], _r["end"]) if _r["start"] else None


def _timeline(roles):
    dated = sorted((r for r in roles if r["start"]), key=lambda r: r["start"])
    total, gaps, span_start, span_end, prev = 0, [], None, None, None
    for r in dated:
        if span_end is None or r["start"] > span_end:
            if span_end is not None:
                total += months_between(span_start, span_end)
                gap = months_between(span_end, r["start"]) - 1
                if gap > 0:
                    gaps.append({{"after": prev, "before": r, "from": span_end, "to": r["start"], "months": gap}})
            span_start, span_end = r["start"], r["end"]
        else:
            span_end = max(span_end, r["end"])
        prev = r if prev is None or r["end"] >= prev["end"] else prev
    if span_end is not None:
        total += months_between(span_start, span_end)
    return {{
        "first_role": dated[0] if dated else None,
        "latest_role": dated[-1] if dated else None,
        "current_roles": [r for r in dated if r["ongoing"]],
        "total_months": total,
        "gaps": gaps,
        "undated_roles": [r for r in roles if not r["start"]],
    }}


# Ready facts (fixed, tested code): first_role, latest_role, current_roles, total_months (overlaps counted once),
# gaps (list of {{after, before, from, to, months}}; consecutive months are not a gap), undated_roles.
TIMELINE = _timeline(ROLES)
'''

AUTHOR_PROMPT = """Write Python 3.13 code (standard library only) that answers the QUESTION.

Your code is appended to this prelude, which is ALREADY defined (do not repeat, redefine or re-parse anything):
- TODAY: date
- ROLES: list of dicts: title, organization, start (date or None), end (date, TODAY when ongoing), ongoing (bool),
  months (int, months in the role), start_text, end_text, month_known (bool). In resume order, not sorted.
- TIMELINE: dict with ready, correct facts:
  first_role (role dict), latest_role (role dict), current_roles (list), total_months (int, overlaps counted once),
  gaps (list of dicts: after, before (role dicts), from, to (dates), months (int)), undated_roles (list).
- months_between(start, end) -> int, years_months(months) -> str ("5 years 8 months"), role_label(role) -> str.
There is NO variable named DATA. ROLES holds {role_count} roles.

Rules:
- Prefer the TIMELINE facts: they are already correct. For example:
  first job -> TIMELINE["first_role"]; total experience -> years_months(TIMELINE["total_months"]);
  gaps -> TIMELINE["gaps"]; time in current role -> each role in TIMELINE["current_roles"] with its "months".
- Only when the TIMELINE facts do not cover the question, compute it from ROLES.
- If ROLES is empty, copy the needed values from the DATA text below into the code exactly as written.
- Never invent values and never do arithmetic in your head.
- If something the question needs is missing, print exactly what is missing.
- Print one short answer sentence first, then the roles and dates it is based on (use role_label and the
  start_text/end_text values). Durations in years and months.
- Plain multi-line Python, no network, no files, no input(). Reply with ONE ```python code block and nothing else.

QUESTION:
{question}

DATA (text, for reference only):
{data}
{feedback}"""

FIX_FEEDBACK = """
Your previous code:
```python
{code}
```
produced:
{output}
Fix the code so that it runs without errors and prints a correct answer."""


def normalize_month(text: Optional[str]) -> tuple[Optional[str], bool]:
    """Free-form resume date -> ("YYYY-MM", month_known). Accepts 2019, 2019-03, 03/2019, Mar 2019, March 2019."""
    value = (text or "").strip().lower()
    if not value or value in _ONGOING:
        return None, False
    if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", value):
        return f"{m[1]}-{int(m[2]):02d}", True
    if m := re.fullmatch(r"(\d{1,2})[-/.](\d{4})", value):
        return f"{m[2]}-{int(m[1]):02d}", True
    if m := re.fullmatch(r"([a-z]{3})[a-z]*\.?,?\s+(\d{4})", value):
        if m[1] in _MONTHS:
            return f"{m[2]}-{_MONTHS[m[1]]:02d}", True
    if m := re.search(r"\b(19|20)(\d{2})\b", value):
        return f"{m[1]}{m[2]}-01", False
    return None, False


def roles_for_code(roles: list[dict]) -> list[dict]:
    """Screening roles -> prelude ROLES input (dates normalised to YYYY-MM; ongoing roles end today)."""
    out = []
    for r in roles:
        start, start_known = normalize_month(r.get("start"))
        end_text = (r.get("end") or "").strip()
        end, end_known = normalize_month(end_text)
        ongoing = not end_text or end_text.lower() in _ONGOING
        out.append({"title": r.get("title") or "", "organization": r.get("organization") or "", "start": start,
                    "end": end, "ongoing": ongoing, "start_text": r.get("start") or "", "end_text": end_text,
                    "month_known": start_known and (end_known or ongoing)})
    return out


def prelude(roles: list[dict]) -> str:
    return PRELUDE.format(today=date.today().isoformat(), roles_json=repr(json.dumps(roles)))


def extract_code(reply: str) -> str:
    """The program from the author's reply: the longest ```python block, or the whole reply when there is none."""
    blocks = _CODE_BLOCK.findall(reply)
    return max(blocks, key=len).strip() if blocks else reply.strip().strip("`").strip()


def _stdout(output: str) -> str:
    return output.split("stdout:\n", 1)[-1].split("\nstderr:\n", 1)[0].strip()


def _succeeded(output: str) -> bool:
    match = _EXIT_CODE.match(output)
    stdout = _stdout(output)
    return bool(match and match.group(1) == "0" and stdout and stdout != "(empty)")


async def calculate(settings: OrchestratorSettings, compute: ComputeClient, budget: StepBudget, question: str,
                    data: str, roles: list[dict]) -> str:
    """Returns text for the assistant: the computed output, or why it could not be computed."""
    author = build_llm()
    code_roles = roles_for_code(roles)
    head = prelude(code_roles)
    feedback, last_output = "", ""
    for attempt in range(1, settings.max_code_attempts + 1):
        if not budget.take("code_author"):
            break
        prompt = AUTHOR_PROMPT.format(role_count=len(code_roles), question=question, data=data, feedback=feedback)
        code = extract_code(str((await invoke_llm(author, prompt)).content))
        if not budget.take("compute.execute_code"):
            break
        try:
            last_output = await compute.execute(f"calc-{uuid.uuid4().hex[:16]}", f"{head}\n\n{code}\n")
        except AppError as e:
            log_event(log, "calculate.compute_failed", code=e.spec.code, attempt=attempt)
            return f"The calculator is unavailable right now ({e.spec.code}). Answer without an exact calculation."
        ok = _succeeded(last_output)
        log_event(log, "calculate.attempt", attempt=attempt, succeeded=ok, code_chars=len(code), roles=len(code_roles))
        if ok:
            return f"Exact result computed with code:\n{_stdout(last_output)}"
        feedback = FIX_FEEDBACK.format(code=code, output=last_output[:2_000])

    if last_output:
        return f"The calculation did not finish successfully. Last output:\n{last_output[:1_500]}"
    return "Step limit reached before the calculation could run. Answer from the information you already have."
