"""Durable workflows: a thin wrapper over the Temporal SDK (docs/impl_temporal.md).

Temporal owns run lifecycle, retries, timeouts, cancellation, waits and replay. This package exists so that
exactly one module in the platform imports the Temporal SDK: services start, signal and query runs through
`WorkflowClient`, and run workers through `run_worker`, without scattering SDK types through business code
(CLAUDE.md section 2 - framework code stays at the edge, business logic stays framework-free and testable
without the framework).

Two rules that are easy to get wrong and expensive to fix later:

* **Payloads are references, never content.** Workflow arguments and results carry ids, hashes and small
  metadata. PDFs, prompts and graph payloads go to object storage or the database; Temporal history is not a
  blob store (docs/impl_domain_platform.md section 5, v3.0 section 15). `MAX_PAYLOAD_BYTES` makes that a check
  rather than a convention.
* **Cancellation propagates.** A Temporal cancel must reach the activity, the MCP call and the LLM stream, so
  an aborted run stops spending money immediately (CLAUDE.md section 6). Activities therefore never swallow
  `asyncio.CancelledError`.

The SDK is an optional dependency (`agent-common[temporal]`); importing this package without it raises a clear
error rather than an ImportError deep in a call stack.
"""
from agent_common.workflow.client import (MAX_PAYLOAD_BYTES, WorkflowClient, WorkflowHandleInfo,
                                          WorkflowStartOptions, check_payload, workflow_id_for)
from agent_common.workflow.runs import JourneyLauncher, JourneyRequest, LaunchedRun
from agent_common.workflow.worker import WorkerSpec, run_worker

# `agent_common.workflow.projection` is deliberately NOT re-exported here: it imports the Temporal SDK at
# module level, and only workers need it. Importing it from a workflow definition module breaks the sandbox.
__all__ = ["MAX_PAYLOAD_BYTES", "JourneyLauncher", "JourneyRequest", "LaunchedRun", "WorkerSpec",
           "WorkflowClient", "WorkflowHandleInfo", "WorkflowStartOptions", "check_payload", "run_worker",
           "workflow_id_for"]
