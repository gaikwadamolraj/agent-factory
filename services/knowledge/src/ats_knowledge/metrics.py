"""Business metrics of the knowledge service (OTEL -> Prometheus). Labels are low-cardinality only."""
from ats_common.telemetry import get_meter

_meter = get_meter("ats_knowledge")

ingest_requests_total = _meter.create_counter("ats_knowledge_ingest_requests_total",
                                              description="Ingest requests by outcome (queued, duplicate, requeued)")
episodes_total = _meter.create_counter("ats_knowledge_episodes_total",
                                       description="Episode extractions by outcome (done, retry, failed)")
extraction_duration = _meter.create_histogram("ats_knowledge_extraction_duration_seconds", unit="s",
                                              description="Time to extract one episode into the graph")
searches_total = _meter.create_counter("ats_knowledge_searches_total",
                                       description="Searches by cache result (hit, miss) and outcome")
search_duration = _meter.create_histogram("ats_knowledge_search_duration_seconds", unit="s",
                                          description="End-to-end search latency, including cache and answer")
answers_total = _meter.create_counter("ats_knowledge_answers_total", description="Grounded answers generated")
