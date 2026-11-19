pip install -q --no-index --find-links /wheels numpy prometheus_client pytest typing_extensions pydantic 2>&1 | grep -v 'Running pip as' | tail -1
cp -r /ro /src && printf '__device_type__ = "A3"\n' > /src/vllm_ascend/_build_info.py && cd /src
PYTHONPATH=/src:/harness python -m pytest -p no:cacheprovider -p t51_realdiag --noconftest -q -rf --continue-on-collection-errors tests/ut/distributed/ascend_store 2>&1 | tail -25
