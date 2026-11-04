"""Builds the autonomous Strands agent for one user: model + skills + tools + persistent memory."""
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.session.repository_session_manager import RepositorySessionManager
from strands.tools.executors import SequentialToolExecutor
from strands.vended_plugins.skills import AgentSkills

from ats_agent.config import AgentSettings
from ats_agent.memory.repository import PostgresRedisSessionRepository
from ats_agent.skills import SkillBundle
from ats_agent.tools import SCREENING_TOOLS

AGENT_ID = "ats-screener"

SYSTEM_PROMPT = """You are the ATS screening agent working for one recruiter (the user).
- For a screening request, activate the matching skill first and follow its workflow exactly.
- For questions about earlier screenings, call screening_history and answer only from its data.
- Never invent scores, skills, or candidates. Answer in plain, short, professional sentences."""


def session_id_for(client_id: int, project_id: int, user_id: int) -> str:
    """Memory is isolated per tenant: one Strands session per (client, project, user)."""
    return f"c{client_id}-p{project_id}-u{user_id}"


def build_agent(
    settings: AgentSettings, session_id: str, skill: SkillBundle, repository: PostgresRedisSessionRepository, model
) -> tuple[Agent, RepositorySessionManager]:
    """Fresh agent bound to a persisted session (memory) and the requested skill version."""
    session_manager = RepositorySessionManager(session_id=session_id, session_repository=repository)
    agent = Agent(
        name="ats-screening-agent",
        agent_id=AGENT_ID,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=list(SCREENING_TOOLS),
        plugins=[AgentSkills(skills=[skill.skill])],
        session_manager=session_manager,
        conversation_manager=SlidingWindowConversationManager(window_size=settings.history_window),
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
    )
    return agent, session_manager
