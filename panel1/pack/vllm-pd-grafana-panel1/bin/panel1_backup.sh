#!/usr/bin/env bash
# Back up an installed panel 1 before changing it: the dashboard files, the provisioning files, prometheus.yml,
# inventory.json and (with --prometheus-container) the rule files. Read-only towards Grafana and Prometheus.
#   bin/panel1_backup.sh <panel-1 folder> [-o <backup folder>] [--prometheus-container <name>]
exec python3 "$(cd "$(dirname "$0")/.." && pwd)/scripts/panel1_files.py" backup "$@"
