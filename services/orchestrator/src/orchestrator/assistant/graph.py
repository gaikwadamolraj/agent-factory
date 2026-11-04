"""LangGraph assistant: an LLM node that decides, and a tool node that acts, looping until the LLM answers.

    START -> assistant --(tool calls?)--> tools -> assistant ... --(no tool calls)--> END

Small models can loop on tools, so the loop is bounded:
* every assistant call and every tool call spends one step of the request's StepBudget (shared with the agents the
  tools call); when only the final answer is left, or after MAX_TOOL_RESULTS tool results, the model is called
  WITHOUT tools and must answer;
* a tool call identical to one already made in this turn is not executed again (the model is told so).
Every LLM call goes through ats_common.llm.guarded (timeout, breaker, catalog errors); cancellation passes through.
"""
import json

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ats_common.context import current_context
from ats_common.llm import Purpose, end_user, gateway_client_args, guarded, model_for
from orchestrator.assistant.prompts import ACTIVE
from orchestrator.services.step_budget import StepBudget

MAX_TOOL_RESULTS = 3
RECURSION_LIMIT = 2 * MAX_TOOL_RESULTS + 4   # assistant/tools node visits, with headroom
STEP_LIMIT_REACHED = ("Step limit for this request reached; this tool was not run. "
                      "Answer with the information you already have.")
ANSWER_NOW = ("You already have the tool results above. Do not call any tool. "
              "Answer the user's question now, using only those results and the chat context.")


def build_llm() -> ChatOpenAI:
    """Chat model through the LLM gateway; the alias follows the current client's model policy."""
    client_id = current_context().client_id
    args = gateway_client_args()
    return ChatOpenAI(model=model_for(Purpose.CHAT, client_id), base_url=args["base_url"], api_key=args["api_key"],
                      timeout=args["timeout"], max_retries=0, temperature=0,
                      model_kwargs={"user": end_user(client_id)} if client_id else {})


def _call_key(call: dict) -> str:
    return f"{call['name']}:{json.dumps(call.get('args', {}), sort_keys=True)}"


async def invoke_llm(llm, messages):
    """One chat LLM call, guarded by ats_common.llm (timeout, breaker, catalog errors, metrics)."""
    return await guarded(Purpose.CHAT, lambda: llm.ainvoke(messages), "llm.assistant")


def build_assistant_graph(tools: list[BaseTool], context: str, budget: StepBudget):
    base_llm = build_llm()
    llm_with_tools = base_llm.bind_tools(tools)
    system = [SystemMessage(ACTIVE), SystemMessage(context)]
    tool_node = ToolNode(tools)

    async def assistant(state: MessagesState) -> dict:
        # History holds only user/assistant text, so ToolMessages in the state belong to this turn.
        tool_results = sum(isinstance(m, ToolMessage) for m in state["messages"])
        # Keep one step for the final answer; without steps left the model must answer without tools.
        if tool_results >= MAX_TOOL_RESULTS or budget.remaining <= 1:
            budget.force("assistant.answer")
            return {"messages": [await invoke_llm(base_llm,
                                               system + state["messages"] + [SystemMessage(ANSWER_NOW)])]}
        budget.take("assistant")
        return {"messages": [await invoke_llm(llm_with_tools, system + state["messages"])]}

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
