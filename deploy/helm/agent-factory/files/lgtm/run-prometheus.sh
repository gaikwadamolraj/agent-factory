#!/bin/bash
# Replaces the image's run-prometheus.sh, which starts Prometheus with no retention at all: the TSDB then grows
# until the pod's /data emptyDir hits its sizeLimit and the kubelet evicts the whole LGTM pod, taking metrics,
# logs and traces down together. RETENTION_PERIOD comes from infra.lgtm.retention.
source ./logging.sh

run_with_logging "Prometheus ${PROMETHEUS_VERSION}" "${ENABLE_LOGS_PROMETHEUS:-false}" ./prometheus/prometheus \
      --web.enable-remote-write-receiver \
      --web.enable-otlp-receiver \
      --enable-feature=exemplar-storage \
      --enable-feature=native-histograms \
      --storage.tsdb.path=/data/prometheus \
      --storage.tsdb.retention.time="${RETENTION_PERIOD:-24h}" \
      --storage.tsdb.retention.size="${PROMETHEUS_RETENTION_SIZE:-512MB}" \
      --config.file=./prometheus.yaml
