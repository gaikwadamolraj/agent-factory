"""Strands tools the agent calls autonomously. Each wraps one screening step and works on the agent's state."""
import json

from strands import ToolContext, tool

from ats_agent.screening.steps import StepContext, StepError, StepRuntime, run_step
from ats_common.errors import AppError
from ats_common.logging import get_logger

log = get_logger(__name__)

WORKSPACE = "workspace"
HISTORY = "history"


async def _run(step: str, tool_context: ToolContext) -> str:
    agent = tool_context.agent
    runtime: StepRuntime = tool_context.invocation_state["runtime"]
    workspace = agent.state.get(WORKSPACE) or {}
    ctx = StepContext(workspace=workspace, runtime=runtime)
    try:
        summary = await run_step(step, ctx, "agent")
        workspace.setdefault("steps_by_agent", []).append(step)
        return summary
    except StepError as e:
        return f"Error: {e}"
    except AppError as e:  # LLM timeout / circuit open: tell the agent, the service decides what to do
        return f"Error: {e.spec.message} {e.spec.hint}"
    except Exception:  # surface failures to the agent without internals; details go to the logs
        log.exception("agent.tool_failed", extra={"step": step})
        return f"Error: {step} failed unexpectedly. Try it again once."
    finally:
        agent.state.set(WORKSPACE, workspace)


@tool(context=True)
async def analyze_job_description(tool_context: ToolContext) -> str:
    """Step 1. Read the loaded job description and turn it into atomic requirements (must-have / nice-to-have)."""
    return await _run("analyze_job_description", tool_context)


@tool(context=True)
async def analyze_candidate_profile(tool_context: ToolContext) -> str:
    """Step 2. Extract verified skills, roles and dates from the loaded resume. Call after analyze_job_description."""
    return await _run("analyze_candidate_profile", tool_context)


@tool(context=True)
async def match_requirements(tool_context: ToolContext) -> str:
    """Step 3. Grade every JD requirement against the candidate's verified skills and compute the score."""
    return await _run("match_requirements", tool_context)


@tool(context=True)
async def write_verdict(tool_context: ToolContext) -> str:
    """Step 4. Write the recruiter summary and interview questions for the graded screening."""
    return await _run("write_verdict", tool_context)


@tool(context=True)
def screening_history(tool_context: ToolContext, limit: int = 10) -> str:
    """Return this user's most recent screenings (newest last): date, chat, role, score, band, recommendation, gaps.

    Args:
        limit: How many recent screenings to return (max 20).
    """
    history = tool_context.agent.state.get(HISTORY) or []
    return json.dumps(history[-max(1, min(limit, 20)):], ensure_ascii=False) if history else "No screenings yet."


SCREENING_TOOLS = [analyze_job_description, analyze_candidate_profile, match_requirements, write_verdict,
                   screening_history]
