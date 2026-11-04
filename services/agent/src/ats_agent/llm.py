"""Strands model for LM Studio (OpenAI-compatible) and resilient, schema-constrained single calls.

Cancellation: when the surrounding task is cancelled (user abort), the in-flight HTTP request is closed, which makes
LM Studio stop generating, so no further tokens are spent.
"""
import time
from typing import TypeVar

import openai
from pydantic import BaseModel
from strands.models.openai import OpenAIModel

from ats_agent import metrics
from ats_agent.config import AgentSettings
from ats_agent.errors import AgentErrors
from ats_common.errors import AppError
from ats_common.resilience import BreakerConfig, CallTimeoutError, CircuitOpenError, RetryPolicy, get_breaker, resilient_call

T = TypeVar("T", bound=BaseModel)
LLM_BREAKER = "llm"
_TRANSIENT = (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError, openai.InternalServerError,
              ConnectionError, TimeoutError)


def build_model(settings: AgentSettings, temperature: float = 0.0) -> OpenAIModel:
    """Model used both by the agent loop and by the step calls."""
    params: dict = {"temperature": temperature}
    if settings.llm_max_tokens:
        params["max_tokens"] = settings.llm_max_tokens
    return OpenAIModel(
        client_args={"base_url": settings.llm_endpoint, "api_key": settings.llm_api_key,
                     "timeout": settings.llm_timeout_s, "max_retries": 0},
        model_id=settings.model_name,
        params=params,
    )


async def _structured_once(model: OpenAIModel, prompt: str, schema: type[T]) -> T:
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    result: T | None = None
    async for event in model.structured_output(schema, messages):
        if "output" in event:
            result = event["output"]
    if result is None:
        raise ValueError(f"Model returned no {schema.__name__}")
    return result


async def structured_call(settings: AgentSettings, model: OpenAIModel, prompt: str, schema: type[T], purpose: str) -> T:
    """One completion constrained by `schema`, with timeout, bounded retry and the shared LLM circuit breaker.

    Raises AppError LLM-TMO-0001 / LLM-UNA-0001 on timeout / open circuit; CancelledError passes through.
    """
    breaker = get_breaker(LLM_BREAKER, BreakerConfig(settings.llm_breaker_failures, settings.llm_breaker_reset_s))
    started, outcome = time.perf_counter(), "ok"
    try:
        return await resilient_call(
            lambda: _structured_once(model, prompt, schema), op=f"llm.{purpose}", timeout_s=settings.llm_timeout_s,
            retry=RetryPolicy(attempts=settings.llm_retry_attempts, retry_on=_TRANSIENT), breaker=breaker,
            is_failure=lambda e: isinstance(e, _TRANSIENT + (CallTimeoutError,)))
    except CallTimeoutError:
        outcome = "timeout"
        raise AppError(AgentErrors.LLM_TIMEOUT)
    except CircuitOpenError as e:
        outcome = "circuit_open"
        raise AppError(AgentErrors.LLM_UNAVAILABLE, retry_after=e.retry_after_s)
    except BaseException as e:
        outcome = "cancelled" if type(e).__name__ == "CancelledError" else "error"
        raise
    finally:
        metrics.llm_calls_total.add(1, {"purpose": purpose, "outcome": outcome})
        metrics.llm_call_duration.record(time.perf_counter() - started, {"purpose": purpose})
