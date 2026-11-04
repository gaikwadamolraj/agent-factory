"""Strands model for the screening agent, through the LLM gateway (ats_common.llm).

The model alias follows the client's model policy (Purpose.SCREENING). Every structured step call is guarded by
ats_common.llm.guarded (timeout, circuit breaker, catalog errors, metrics), the same as every other LLM call.

Cancellation: when the surrounding task is cancelled (user abort), the in-flight HTTP request is closed and the
gateway stops the upstream generation, so no further tokens are spent.
"""
from typing import Optional, TypeVar

from ats_common.llm import Purpose, end_user, gateway_client_args, guarded, model_for
from pydantic import BaseModel
from strands.models.openai import OpenAIModel

T = TypeVar("T", bound=BaseModel)


def build_model(client_id: Optional[int] = None) -> OpenAIModel:
    """Model used both by the agent loop and by the step calls, for this client's screening alias."""
    params: dict = {"temperature": 0.0}
    if client_id:
        params["user"] = end_user(client_id)
    return OpenAIModel(client_args=gateway_client_args(), model_id=model_for(Purpose.SCREENING, client_id),
                       params=params)


async def _structured_once(model: OpenAIModel, prompt: str, schema: type[T]) -> T:
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    result: T | None = None
    async for event in model.structured_output(schema, messages):
        if "output" in event:
            result = event["output"]
    if result is None:
        raise ValueError(f"Model returned no {schema.__name__}")
    return result


async def structured_call(model: OpenAIModel, prompt: str, schema: type[T], purpose: str) -> T:
    """One completion constrained by `schema`. Raises AppError LLM-TMO-0001 / LLM-UNA-0001 / LLM-INV-0001."""
    return await guarded(Purpose.SCREENING, lambda: _structured_once(model, prompt, schema), f"llm.{purpose}")
