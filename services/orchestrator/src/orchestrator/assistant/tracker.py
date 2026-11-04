"""Progress events for one chat turn, streamed to the browser as Server-Sent Events."""
import asyncio
import json
import time
from typing import Optional

from orchestrator.constants import TurnEvent

MAX_STEPS = 50      # steps kept for the turn record

class TurnTracker:
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.started = time.monotonic()
        self.stage = "received"
        self.stage_started = self.started
        self.message = "Request received"
        self.progress = 0.0
        self.steps: list[dict] = []         # [{stage, atS}] for the turn record (bounded by MAX_STEPS)

    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 1)

    def push(self, event: dict) -> None:
        event.setdefault("turnId", self.turn_id)
        event.setdefault("elapsed", self.elapsed())
        self.queue.put_nowait(event)

    def emit(self, stage: str, message: str, progress: Optional[float] = None, **details) -> None:
        """A progress step (stage ids are listed in the frontend's ProgressTimeline)."""
        if stage != self.stage:
            self.stage_started = time.monotonic()
            if len(self.steps) < MAX_STEPS:
                self.steps.append({"stage": stage, "atS": self.elapsed()})
        self.stage, self.message = stage, message
        if progress is not None:
            self.progress = max(self.progress, float(progress))
        self.push({"type": TurnEvent.PROGRESS, "stage": stage, "message": message,
                   "progress": round(self.progress, 1), "details": details})

    def heartbeat(self) -> dict:
        return {"type": TurnEvent.HEARTBEAT, "turnId": self.turn_id, "stage": self.stage, "message": self.message,
                "progress": round(self.progress, 1), "elapsed": self.elapsed(),
                "stageElapsed": round(time.monotonic() - self.stage_started, 1)}


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
