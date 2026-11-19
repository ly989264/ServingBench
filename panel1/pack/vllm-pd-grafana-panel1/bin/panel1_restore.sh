#!/usr/bin/env bash
# Put back what bin/panel1_backup.sh saved, byte for byte, and make Grafana and Prometheus read it again.
#   bin/panel1_restore.sh <backup folder> [--grafana-url URL] [--prometheus-url URL]
# Grafana account (server admin) from GRAFANA_USER + GRAFANA_PASSWORD, or GRAFANA_TOKEN.
exec python3 "$(cd "$(dirname "$0")/.." && pwd)/scripts/panel1_files.py" restore "$@"
