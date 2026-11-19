export HCCL_IF_IP=10.1.2.3
export GLOO_SOCKET_IFNAME=eth0
export VLLM_LOGGING_LEVEL="info"
export MOONCAKE_CONFIG_PATH=/launch/mooncake.json
export GLOG_logtostderr=1
export API_TOKEN=supersecretvalue123
export PATH=/opt/t51bin:$PATH

export ASCEND_RT_VISIBLE_DEVICES=$1
KV='{"kv_connector":"MultiConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"connectors":[{"kv_connector":"MooncakeConnectorV1","kv_role":"kv_producer","kv_port":"30000","engine_id":"0"},{"kv_connector":"AscendStoreConnector","kv_role":"kv_both","kv_connector_extra_config":{"backend":"mooncake","load_async":false}}]}}'

vllm serve /models/glm53 \
    --host 0.0.0.0 \
    --port $2 \
    --data-parallel-size $3 \
    --data-parallel-rank $4 \
    --data-parallel-address $5 \
    --data-parallel-rpc-port $6 \
    --tensor-parallel-size $7 \
    --enable-expert-parallel \
    --kv-transfer-config "$KV" \
    --speculative-config '{"method":"deepseek_mtp","num_speculative_tokens":1,"enforce_eager":true}' \
    > /logs/vllm_$2.log 2>&1
