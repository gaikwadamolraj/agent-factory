"""Running chat turns, so a user can abort one.

Abort = cancel the turn's asyncio task. The cancellation propagates to the assistant LLM call and, through the
agent's `cancel_request` tool, to the agent's LLM call - nothing keeps generating tokens after an abort.
A Redis flag is also set so a replica that does not own the task can see the abort.
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional

from agent_common.redis_keys import Keys, build_key, ttl


@dataclass
class RunningTurn:
    turn_id: str
    chat_id: str
    user_id: int
    client_id: int
    project_id: int
    task: Optional[asyncio.Task] = None
    agent_request_ids: set[str] = field(default_factory=set)   # in-flight agent requests to cancel on abort
    knowledge_request_ids: set[str] = field(default_factory=set)   # in-flight knowledge searches to cancel
    aborted: bool = False


class TurnRegistry:
    def __init__(self, redis_client, env: str):
        self.redis, self.env = redis_client, env
        self._turns: dict[str, RunningTurn] = {}

    @staticmethod
    def new_turn_id() -> str:
        return uuid.uuid4().hex

    def start(self, turn: RunningTurn) -> None:
        self._turns[turn.turn_id] = turn

    def finish(self, turn_id: str) -> None:
        self._turns.pop(turn_id, None)

    def running_for_chat(self, chat_id: str) -> Optional[RunningTurn]:
        return next((t for t in self._turns.values() if t.chat_id == chat_id), None)

    def get(self, turn_id: str) -> Optional[RunningTurn]:
        return self._turns.get(turn_id)

    async def abort(self, turn: RunningTurn) -> None:
        """Mark aborted (visible to all replicas) and cancel the local task."""
        turn.aborted = True
        await self.redis.setex(build_key(Keys.TURN_ABORT, self.env, turn_id=turn.turn_id), ttl(Keys.TURN_ABORT), "1")
        if turn.task and not turn.task.done():
            turn.task.cancel()

    async def is_aborted(self, turn_id: str) -> bool:
        return bool(await self.redis.get(build_key(Keys.TURN_ABORT, self.env, turn_id=turn_id)))
