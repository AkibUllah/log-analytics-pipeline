#!/usr/bin/env bash
# Codespaces demo: synthesise a day of nginx traffic, ingest it, render the HTML report
# and serve both the report (:8000) and the read-only stats API (:8080) in the background.
set -euo pipefail
cd "$(dirname "$0")/.."

export LOGPIPE_DB_PATH="${LOGPIPE_DB_PATH:-./logpipe.duckdb}"
export LOGPIPE_CHECKPOINT_PATH="${LOGPIPE_CHECKPOINT_PATH:-./logpipe.checkpoint.json}"

if [ ! -s data/access.log ]; then
  logpipe generate-sample --lines 50000 --out data/access.log
fi
logpipe ingest data/access.log
mkdir -p out
logpipe report --since 24h --html out/index.html

nohup python -m http.server 8000 --directory out > /tmp/report-server.log 2>&1 &
nohup logpipe serve --host 0.0.0.0 --port 8080 > /tmp/stats-api.log 2>&1 &
echo "Report on port 8000, stats API on port 8080 (try /docs and /v1/stats?since=1h)."
