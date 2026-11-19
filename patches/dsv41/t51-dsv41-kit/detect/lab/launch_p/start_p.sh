#!/bin/bash
cd /launch
python3 launch_online_dp.py --dp-size 8 --tp-size 2 --dp-size-local 2 --dp-rank-start 2 --dp-address 10.1.2.3 --dp-rpc-port 12890 --vllm-start-port 9000
