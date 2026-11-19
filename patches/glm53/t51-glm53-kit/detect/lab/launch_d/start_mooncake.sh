#!/bin/bash
mooncake_master --port 50088 --rpc_address=10.1.2.4 --eviction_high_watermark_ratio 0.9 --enable_http_metadata_server=true --http_metadata_server_port 8080
