#!/bin/bash
# Run on the monitoring machine. Over SSH, on every P and D machine of inventory.json or on the ones named:
#   bin/nodes.sh check [address ...]       preflight, one line per machine
#   bin/nodes.sh paths                     no SSH: folders, instance names and (NODE_MODE=vllm) launch-script additions, per machine
#   bin/nodes.sh push [address ...]        sends the three node files to NODE_CONF_DIR; replaces only those whose sha256 differs
#   bin/nodes.sh selfcheck [address ...]   runs the node self-check against the pushed files; vllm: also the running vLLM containers
#   bin/nodes.sh start [address ...]       NODE_MODE=mock: MOCK_RATE guard, pushes the files, stops what runs, starts the mock writer
#   bin/nodes.sh stop [address ...]        stops the mock from here: SIGTERM, up to 10 s, SIGKILL; FAIL with the pids if any is left
#   bin/nodes.sh clean [address ...]       mock: the mock, its logs (slowly), the node files; vllm: only the mock folders
#   bin/nodes.sh status [address ...]      mock: FAIL when a machine's mock processes are not as many as its instances
#   bin/nodes.sh mock-clean [address ...]  mock: stops the mock and deletes the logs it wrote (bin/clear_data.sh uses it)
. "$(dirname "$0")/lib.sh"
load_env
export NODE_PYTHON=${DEPLOY_PYTHON:-}
need_python
need_inventory
exec "$PYTHON" "$PACK_DIR/scripts/nodes.py" "$@"
