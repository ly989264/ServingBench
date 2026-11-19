#!/bin/bash
cd /launch
python3 launch_online_dp.py --dp-size 64 --tp-size 1 --dp-size-local 16 --dp-rank-start 16 --dp-address 10.1.2.6 --dp-rpc-port 12890 --vllm-start-port 9000
