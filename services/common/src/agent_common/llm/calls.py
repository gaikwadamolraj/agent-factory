"""Every LLM request of the platform goes through here: one OpenAI client for the gateway, one guard (timeout,
circuit breaker, catalog errors, metrics), structured completions and embeddings.

Frameworks (Strands, LangChain, Graphiti) build their own clients from `gateway_client_args()` and wrap their calls
in `guarded()`, so endpoint, key, timeouts, breaker and metrics are the same everywhere.

Retries across backends happen in the gateway (LiteLLM router), so clients do not retry transport errors; they only
retry a malformed reply (small models sometimes break JSON) once.
"""
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Optional, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from agent_common.errors import AppError, Errors
from agent_common.llm import metrics
from agent_common.llm.models import Purpose, model_for
from agent_common.llm.settings import LLMSettings, get_llm_settings
from agent_common.resilience import (BreakerConfig, CallTimeoutError, CircuitOpenError, RetryPolicy, get_breaker,
                                   is_llm_format_error, resilient_call)

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)
_MALFORMED = (ValidationError, ValueError)          # pydantic ValidationError is a ValueError


def gateway_client_args(settings: Optional[LLMSettings] = None) -> dict:
    """Arguments for any OpenAI-compatible client pointed at the gateway."""
    settings = settings or get_llm_settings()
    return {"base_url": settings.gateway_url, "api_key": settings.gateway_key.get_secret_value(),
            "timeout": settings.timeout_s, "max_retries": 0}


@lru_cache
def gateway_client() -> AsyncOpenAI:
    """Process-wide async OpenAI client for the gateway (connection pool reused)."""
    return AsyncOpenAI(**gateway_client_args())


def end_user(client_id: Optional[int]) -> Optional[str]:
    """Tenant tag sent as OpenAI `user`: the gateway attributes usage per client. Never a person's identity."""
    return f"client-{client_id}" if client_id else None


async def guarded(purpose: Purpose, call: Callable[[], Awaitable[T]], op: str,
                  timeout_s: Optional[float] = None, retry_malformed: bool = True) -> T:
    """Run one LLM call with timeout + the purpose's circuit breaker + metrics.

    Raises AppError LLM-TMO-0001 (timeout), LLM-UNA-0001 (circuit open), LLM-INV-0001 (malformed reply after a retry).
    asyncio.CancelledError passes through (closing the HTTP request makes the gateway stop the upstream call).
    """
    settings = get_llm_settings()
    breaker = get_breaker(f"llm.{purpose.value}", BreakerConfig(settings.breaker_failures, settings.breaker_reset_s))
    retry = RetryPolicy(attempts=2 if retry_malformed else 1, retry_on=_MALFORMED, retry_if=is_llm_format_error)
    started, outcome = time.perf_counter(), "ok"
    try:
        return await resilient_call(call, op=op, timeout_s=timeout_s or settings.timeout_s, retry=retry,
                                    breaker=breaker, is_failure=lambda e: not isinstance(e, _MALFORMED + (AppError,)))
    except CallTimeoutError:
        outcome = "timeout"
        raise AppError(Errors.LLM_TIMEOUT) from None
    except CircuitOpenError as e:
        outcome = "circuit_open"
        raise AppError(Errors.LLM_UNAVAILABLE, retry_after=e.retry_after_s) from None
    except _MALFORMED:
        outcome = "malformed"
        raise AppError(Errors.LLM_MALFORMED) from None
    except openai.APIConnectionError:
        outcome = "unavailable"
        raise AppError(Errors.LLM_UNAVAILABLE, retry_after=10) from None
    except openai.APIStatusError as e:
        if e.status_code == 429 or e.status_code >= 500:   # gateway: every backend busy, cooling down or failing
            outcome = "unavailable"
            raise AppError(Errors.LLM_UNAVAILABLE, retry_after=10) from None
        outcome = "rejected"
        raise
    except BaseException as e:
        outcome = "cancelled" if type(e).__name__ == "CancelledError" else "error"
        if is_llm_format_error(e):
            outcome = "malformed"
            raise AppError(Errors.LLM_MALFORMED) from None
        raise
    finally:
        metrics.llm_calls_total.add(1, {"purpose": purpose.value, "op": op, "outcome": outcome})
        metrics.llm_call_duration.record(time.perf_counter() - started, {"purpose": purpose.value})


async def structured_completion(purpose: Purpose, prompt: str, schema: type[M], *, client_id: Optional[int] = None,
                                op: str = "", max_tokens: Optional[int] = None) -> M:
    """One JSON-schema-constrained completion through the gateway, validated into `schema`."""
    settings = get_llm_settings()

    async def once() -> M:
        response = await gateway_client().chat.completions.create(
            model=model_for(purpose, client_id), temperature=0.0,
            max_tokens=max_tokens or settings.max_output_tokens, user=end_user(client_id),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}})
        return schema.model_validate_json(response.choices[0].message.content or "{}")

    return await guarded(purpose, once, op or f"llm.{purpose.value}")
