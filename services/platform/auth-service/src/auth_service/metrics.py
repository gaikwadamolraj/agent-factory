"""Business metrics of the auth-service (OTEL -> Prometheus -> Grafana). Labels are low-cardinality enums and
service names only; usernames live in the logs as digests."""
from agent_common.telemetry import get_meter

_meter = get_meter("ats_auth")

# outcome: ok | failed | throttled | refreshed (same metric name as before the move, so dashboards keep working)
logins_total = _meter.create_counter("agent_logins_total", description="Sign-in attempts by outcome")
# client, audience: service names from the policy; outcome: ok | bad_credentials | forbidden | throttled
service_tokens_total = _meter.create_counter("agent_service_tokens_total",
                                             description="client_credentials token requests by client and outcome")
