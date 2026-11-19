#!/bin/bash
cd /launch
python3 launch_online_dp.py --dp-size 16 --tp-size 2 --dp-size-local 8 --dp-rank-start 8 --dp-address 10.1.2.3 --dp-rpc-port 12890 --vllm-start-port 9000
