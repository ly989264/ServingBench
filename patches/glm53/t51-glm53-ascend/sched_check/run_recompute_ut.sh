# Runs vllm-ascend's own unit tests under tests/ut/core (all scheduler tests), on the original and on the ported file.
# Optional T51_EXTRA_PATH: extra entries appended to PYTHONPATH (packages missing from the image).
# Same image and mounts as run_sched_fieldset.sh. Each twice: with vLLM's Scheduler as the name the tests import, and
# with BalanceScheduler in its place (t51_vllm_base.py / t51_balance_base.py; both assert the base they set).
# Output: /out/ut-{original,ported}-{vllm,balance}.txt.
set -e
F=vllm_ascend/core/recompute_scheduler.py
TESTS="${TESTS:-tests/ut/core}"  # every scheduler test of vllm-ascend
cp -r /vllm /work-vllm
for base in vllm balance; do
PLUGINS="-p t51_scipy_stub -p t51_${base}_base"
for variant in original ported; do
  rm -rf /work-ascend && cp -r /ascend /work-ascend && cd /work-ascend
  [ -f vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > vllm_ascend/_build_info.py
  grep "  $F$" /pkg/SHA256SUMS.before | sha256sum -c
  if [ $variant = ported ]; then
    git apply --unsafe-paths --directory=. /pkg/per-file/11-core-recompute_scheduler.patch
    grep "  $F$" /pkg/SHA256SUMS.after | sha256sum -c
  fi
  HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu VLLM_VERSION=0.30.0 PYTHONPATH=/work-ascend:/work-vllm:/check${T51_EXTRA_PATH:+:$T51_EXTRA_PATH} \
    python -m pytest $PLUGINS -p no:cacheprovider -q -rA $TESTS > /out/ut-$variant-$base.txt 2>&1 || true
  tail -1 /out/ut-$variant-$base.txt
done
done
