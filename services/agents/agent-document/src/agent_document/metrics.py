"""Business metrics of the document agent (OTEL -> Prometheus). Labels are low-cardinality only."""
from agent_common.telemetry import get_meter

_meter = get_meter("agent_document")

documents_read_total = _meter.create_counter("agent_documents_read_total",
                                             description="Documents extracted, by kind (pdf/docx/xlsx) and outcome")
read_duration = _meter.create_histogram("agent_document_read_duration_seconds", unit="s",
                                        description="Time to extract one document, by kind")
chunks_total = _meter.create_counter("agent_document_chunks_total", description="Chunks produced by chunk_document")
injection_flags = _meter.create_counter("agent_prompt_injection_detected_total",
                                     description="Document text that tried to instruct an LLM, by source")
embeddings_skipped = _meter.create_counter("agent_document_embeddings_skipped_total",
                                          description="chunk_document calls where embedding was unavailable")
