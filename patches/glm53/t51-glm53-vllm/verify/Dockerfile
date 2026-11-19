FROM t14p2-vllm-cpu:v032-pd032
RUN /opt/venv/bin/pip uninstall -y vllm && rm -rf /opt/venv/lib/python3.12/site-packages/vllm && \
    /opt/venv/bin/pip install --no-cache-dir pytest pytest-asyncio tblib pytest-mock pytest-timeout setuptools-scm setuptools-rust wheel && \
    (apt-get update >/dev/null 2>&1 && apt-get install -y --no-install-recommends git >/dev/null 2>&1 || true)
ENV LD_PRELOAD=""
