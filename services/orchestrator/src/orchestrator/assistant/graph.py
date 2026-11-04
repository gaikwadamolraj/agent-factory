"""LangGraph assistant: an LLM node that decides, and a tool node that acts, looping until the LLM answers.

    START -> assistant --(tool calls?)--> tools -> assistant ... --(no tool calls)--> END

Small models can loop on tools, so the loop is bounded:
* every assistant call and every tool call spends one step of the request's StepBudget (shared with the agents the
  tools call); when only the final answer is left, or after MAX_TOOL_RESULTS tool results, the model is called
  WITHOUT tools and must answer;
* a tool call identical to one already made in this turn is not executed again (the model is told so).
Every LLM call has a timeout and goes through the 'assistant-llm' circuit breaker; cancellation passes through.
"""
import json

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ats_common.errors import AppError, Errors
from ats_common.resilience import (BreakerConfig, CallTimeoutError, CircuitOpenError, get_breaker, is_llm_format_error,
                                   resilient_call)
from orchestrator.assistant.prompts import ACTIVE
from orchestrator.config import OrchestratorSettings
from orchestrator.services.step_budget import StepBudget

MAX_TOOL_RESULTS = 3
RECURSION_LIMIT = 2 * MAX_TOOL_RESULTS + 4   # assistant/tools node visits, with headroom
STEP_LIMIT_REACHED = ("Step limit for this request reached; this tool was not run. "
                      "Answer with the information you already have.")
ANSWER_NOW = ("You already have the tool results above. Do not call any tool. "
              "Answer the user's question now, using only those results and the chat context.")


def build_llm(settings: OrchestratorSettings) -> ChatOpenAI:
    return ChatOpenAI(model=settings.model_name, base_url=settings.llm_endpoint, api_key=settings.llm_api_key,
                      temperature=0, timeout=settings.llm_timeout_s, max_retries=1)


def _call_key(call: dict) -> str:
    return f"{call['name']}:{json.dumps(call.get('args', {}), sort_keys=True)}"


async def invoke_llm(settings: OrchestratorSettings, llm, messages):
    """One LLM call with timeout + circuit breaker (errors become catalog AppErrors)."""
    breaker = get_breaker("assistant-llm", BreakerConfig(settings.llm_breaker_failures, settings.llm_breaker_reset_s))
    try:
        return await resilient_call(lambda: llm.ainvoke(messages), op="llm.assistant", timeout_s=settings.llm_timeout_s,
                                    breaker=breaker)
    except CallTimeoutError:
        raise AppError(Errors.LLM_TIMEOUT)
    except CircuitOpenError as e:
        raise AppError(Errors.LLM_UNAVAILABLE, retry_after=e.retry_after_s)
    except Exception as e:
        # LM Studio rejects malformed tool calls with a 500 ("...does not match the expected ... format"); the client
        # already retried once (max_retries=1). Never show the raw server text to the user.
        if is_llm_format_error(e):
            raise AppError(Errors.LLM_MALFORMED) from None
        raise


def build_assistant_graph(settings: OrchestratorSettings, tools: list[BaseTool], context: str,
                          budget: StepBudget):
    base_llm = build_llm(settings)
    llm_with_tools = base_llm.bind_tools(tools)
    system = [SystemMessage(ACTIVE), SystemMessage(context)]
    tool_node = ToolNode(tools)

    async def assistant(state: MessagesState) -> dict:
        # History holds only user/assistant text, so ToolMessages in the state belong to this turn.
        tool_results = sum(isinstance(m, ToolMessage) for m in state["messages"])
        # Keep one step for the final answer; without steps left the model must answer without tools.
        if tool_results >= MAX_TOOL_RESULTS or budget.remaining <= 1:
            budget.force("assistant.answer")
            return {"messages": [await invoke_llm(settings, base_llm,
                                               system + state["messages"] + [SystemMessage(ANSWER_NOW)])]}
        budget.take("assistant")
        return {"messages": [await invoke_llm(settings, llm_with_tools, system + state["messages"])]}

    async def run_tools(state: MessagesState) -> dict:
        """Execute new tool calls; answer repeated identical calls without running them again."""
        previous = {_call_key(c) for m in state["messages"][:-1] if isinstance(m, AIMessage)
                    for c in (m.tool_calls or [])}
        last: AIMessage = state["messages"][-1]
        repeated = [c for c in last.tool_calls if _call_key(c) in previous]
        candidates = [c for c in last.tool_calls if _call_key(c) not in previous]
        fresh = [c for c in candidates if budget.take(f"tool:{c['name']}")]
        over_budget = [c for c in candidates if c not in fresh]
        messages = [ToolMessage(content=f"Already called {c['name']} with the same arguments in this turn; "
                                        f"use that earlier result.", tool_call_id=c["id"]) for c in repeated]
        messages += [ToolMessage(content=STEP_LIMIT_REACHED, tool_call_id=c["id"]) for c in over_budget]
        if fresh:
            result = await tool_node.ainvoke({"messages": [last.model_copy(update={"tool_calls": fresh})]})
            messages.extend(result["messages"])
        return {"messages": messages}

    graph = StateGraph(MessagesState)
    graph.add_node("assistant", assistant)
    graph.add_node("tools", run_tools)
    graph.add_edge(START, "assistant")
    graph.add_conditional_edges("assistant", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "assistant")
    return graph.compile()
