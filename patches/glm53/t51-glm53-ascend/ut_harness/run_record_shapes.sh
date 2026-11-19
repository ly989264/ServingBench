# Runs inside python:3.12-slim. Mounts: /ro (tree), /wheels, /harness, /vllm01, /out (writable).
# Usage: sh /harness/run_record_shapes.sh NAME   -> /out/NAME.json
pip install -q --no-index --find-links /wheels numpy prometheus_client pytest typing_extensions pydantic 2>&1 | grep -v 'Running pip as' | tail -1
cp -r /ro /src && printf '__device_type__ = "A3"\n' > /src/vllm_ascend/_build_info.py && cd /src
export VLLM_COMPACT_ROOT=/vllm01
PYTHONPATH=/src:/harness python /harness/t51_record_shapes.py /out/$1.json
