export HCCL_IF_IP=10.1.2.3
export GLOO_SOCKET_IFNAME=eth0
export VLLM_LOGGING_LEVEL="info"
export MOONCAKE_CONFIG_PATH=/launch/mooncake.json
export GLOG_logtostderr=1
export API_TOKEN=supersecretvalue123
export PATH=/opt/t51bin:$PATH

export ASCEND_RT_VISIBLE_DEVICES=$1
KV='{"kv_connector":"MultiConnector","kv_role":"kv_producer","kv_load_failure_policy":"recompute","kv_connector_extra_config":{"connectors":[{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_producer","engine_id":"0"},{"kv_connector":"AscendStoreConnector","kv_role":"kv_both","kv_connector_extra_config":{"backend":"mooncake","load_async":true}}]}}'

vllm serve /models/dsv41 \
    --host 0.0.0.0 \
    --port $2 \
    --data-parallel-size $3 \
    --data-parallel-rank $4 \
    --data-parallel-address $5 \
    --data-parallel-rpc-port $6 \
    --tensor-parallel-size $7 \
    --enable-expert-parallel \
    --async-scheduling \
    --kv-transfer-config "$KV" \
    --speculative-config '{"method":"dspark","num_speculative_tokens":5}' \
    > /logs/vllm_$2.log 2>&1
