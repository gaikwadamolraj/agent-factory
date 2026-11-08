# Abort: stopping a turn down to the LLM

Goal: when a user presses **Stop**, no component keeps generating tokens.

```
Browser  --POST /api/v1/chats/{chatId}/turns/{turnId}/abort-->  Orchestrator
   |                                                               | TurnRegistry.abort(turn)
   | (or closes the SSE stream)                                    |  - Redis flag turn:{id}:abort (other replicas)
   v                                                               |  - task.cancel()
Next.js proxy (forwards req.signal) ------------------------------>|
                                                                   v
                                         _run_turn catches CancelledError
                                           1. assistant LLM call (ChatOpenAI) is cancelled -> HTTP closed
                                           2. MCP call to the agent is cancelled -> stream closed
                                           3. cancel_request(requestId) sent to the agent for every
                                              in-flight agent request (belt and braces)
                                           4. "Stopped" message stored (errorCode WFL-CAN-0001)
                                           5. SSE event `aborted`
                                                                   |
Agent (MCP server) <-----------------------------------------------+
   handler task cancelled (stream closed) or AgentService.cancel(requestId)
   -> CancelledError through the Strands loop and the step's structured call
   -> httpx request to LM Studio closed -> LM Studio stops generating
```

Details
* `turnId` is sent in the first SSE event (`turn_started`); `runningTurnId` is also returned by `GET /chats/{id}`.
* The agent request id is registered on the `RunningTurn` before the MCP call and removed only after success, so
  it is still known when the abort handler runs.
* `resilient_call` never retries or swallows `CancelledError`.
* Observability: `agent_turn_aborts_total`, `agent_cancellations_total`,
  `agent_screenings_total{outcome="cancelled"}`; logs `agent.screening_cancelled`, `agent.cancel_not_running`
  (the explicit cancel arrived after the stream close had already stopped the work).
