# Runs inside python:3.12-slim. Mounts: /ro (vllm-ascend tree, read-only), /wheels, /harness, /vllm01 (VLLM_COMPACT_ROOT).
# Usage: sh /harness/run_in_container.sh [pytest target ...]; env PYTEST_K selects tests, TAIL_LINES sets output length.
pip install -q --no-index --find-links /wheels numpy prometheus_client pytest typing_extensions pydantic 2>&1 | grep -v 'Running pip as' | tail -1
cp -r /ro /src && printf '__device_type__ = "A3"\n' > /src/vllm_ascend/_build_info.py && cd /src
export VLLM_COMPACT_ROOT=/vllm01
TARGETS="${*:-tests/ut/distributed/ascend_store}"
PYTHONPATH=/src:/harness python -m pytest -p no:cacheprovider -p t51_realdiag -p t51_glm_stubs --noconftest -q -rf --continue-on-collection-errors ${PYTEST_K:+-k "$PYTEST_K"} $TARGETS 2>&1 | tail -${TAIL_LINES:-40}
