# The checks that run on Python 3.11 (the site runs 3.11.10). Image: python:3.11-slim.
# Mounts: /tree (vllm-ascend d4ad49796 + the package [+ p2d], ro), /vllm (vLLM tree carrying
#         vllm/v1/metrics/pd_diagnostics.py, ro), /kit (this repository, ro), /out; for B also /wheels (pure-Python wheels
#         of the 3.12 runs) and /wheels311 (numpy 2.2.6 and pydantic_core 2.27.2 for cp311 aarch64, sha256 as on PyPI).
# Usage:  sh /kit/tools/py311_check.sh A     syntax of every file, stdlib-only tests, static runner points (no network)
#         sh /kit/tools/py311_check.sh B     tests/ut/distributed/ascend_store with the stub harness; installs the
#                                            test-only packages, same versions as the 3.12 runs, from /wheels*
set -e
PART=${1:-A}
python3 --version
cp -r /tree /src && printf '__device_type__ = "A3"\n' > /src/vllm_ascend/_build_info.py && cd /src
if [ "$PART" = A ]; then
  echo "== syntax (ast.parse + compile) on $(python3 --version)"
  {
    cut -d' ' -f3 /kit/patches/vllm-ascend/SHA256SUMS.after
    git apply --stat /kit/patches/vllm-ascend/per-file/90-tests.patch 2>/dev/null | awk '{print $1}' | grep '\.py$' || true
    grep '^+++ b/' /kit/patches/vllm-ascend/per-file/90-tests.patch | sed 's#^+++ b/##'
  } | sort -u > /tmp/package_files.txt
  n=0; bad=0
  for f in $(cat /tmp/package_files.txt); do
    n=$((n + 1)); python3 -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "$f" || bad=$((bad + 1))
  done
  echo "package files: $n, syntax errors: $bad"
  n=0; bad=0
  for f in $(cd /kit && find joint sched_check tools ut_harness probe patches/vllm-ascend/replay -name '*.py' | sort); do
    n=$((n + 1)); python3 -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "/kit/$f" || bad=$((bad + 1))
  done
  echo "kit scripts: $n, syntax errors: $bad"
  echo "== stdlib-only tests (python -m unittest)"
  for t in tests/ut/worker/test_pd_device_timing.py tests/ut/worker/test_pd_diag_runner_points.py \
           tests/ut/distributed/ascend_store/test_wait_for_put_standalone.py \
           tests/ut/distributed/ascend_store/test_pool_load_link_standalone.py; do
    m=$(echo "$t" | sed 's#/#.#g; s#\.py$##')
    echo "-- $t"
    VLLM_COMPACT_ROOT=/vllm python3 -m unittest "$m" 2>&1 | tail -3
  done
  echo "== static runner points"
  python3 -I /kit/tools/check_runner_points.py /src
else
  pip install -q --no-index --find-links /wheels --find-links /wheels311 numpy==2.2.6 prometheus_client==0.21.1 \
    pytest==8.3.5 typing_extensions==4.12.2 pydantic==2.10.6 2>&1 | grep -v -E 'Running pip as|notice' | tail -2 || true
  export VLLM_COMPACT_ROOT=/vllm
  PYTHONPATH=/src:/kit/ut_harness python3 -m pytest -p no:cacheprovider -p t51_realdiag -p t51_glm_stubs --noconftest \
    -q -rf --continue-on-collection-errors tests/ut/distributed/ascend_store 2>&1 | tail -${TAIL_LINES:-90}
fi
