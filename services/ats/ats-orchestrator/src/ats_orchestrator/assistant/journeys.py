"""Journeys: what one chat turn does, declared in one place. A journey names its skill (versioned prompt or agent
skill) and which building blocks it uses; the runner reads this instead of branching on ad-hoc flags, and every turn
record says which journey (and skill version) handled it.

    screen  "Screen candidate" button: the screening agent runs its skill (4 verified steps); the reply is built in
            code from the stored result. No chat model, no tools, no memory.
    ask     a chat question: grounding (resume/JD excerpts + this candidate's graph facts) -> chat model with tools
            (search, screening details, calculate, knowledge graph) -> score guard. Uses the chat memory.

Add a journey: add a Journey here (and its prompt as a new versioned constant), then handle its name in the runner.
"""
from dataclasses import dataclass

from ats_orchestrator.assistant import memory, prompts
from ats_orchestrator.constants import TurnAction


@dataclass(frozen=True)
class Journey:
    name: str
    skill: str          # versioned skill that answers: prompt constant or agent skill
    chat_model: bool    # a chat-model loop with tools writes the answer
    grounding: bool     # document excerpts + graph facts attached to the question
    memory: bool        # conversation memory read before and updated after the turn


SCREEN = Journey("screen", skill="agent:ats-screening", chat_model=False, grounding=False, memory=False)
ASK = Journey("ask", skill=f"assistant:{prompts.ACTIVE_VERSION}+{memory.MEMORY_VERSION}", chat_model=True,
              grounding=True, memory=True)

JOURNEYS: dict[TurnAction, Journey] = {TurnAction.SCREEN: SCREEN, TurnAction.CHAT: ASK}


def journey_for(action: TurnAction) -> Journey:
    """The journey of a turn action."""
    return JOURNEYS[action]
