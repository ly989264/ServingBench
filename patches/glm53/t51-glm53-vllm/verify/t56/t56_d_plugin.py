"""Run upstream scheduler tests with every scheduler judged D: is_pd_decode() returns True, so every
kv_ready_wait hook runs. Nothing else changes. T56_D_COUNTS: file for how often the hooks ran."""
import os


def pytest_configure(config):
    from vllm.v1.core.sched.scheduler import Scheduler
    from vllm.v1.metrics import pd_diagnostics as diag

    diag.is_pd_decode = lambda vllm_config: True
    counts = config._t56_counts = {}
    for name in ("_pd_kv_ready_seen", "_pd_kv_ready_scheduled", "_pd_kv_load_failed", "_pd_kv_ready_finish"):
        original = getattr(Scheduler, name)

        def counted(self, *a, _o=original, _n=name, **k):
            counts[_n] = counts.get(_n, 0) + 1
            return _o(self, *a, **k)

        setattr(Scheduler, name, counted)


def pytest_unconfigure(config):
    path = os.environ.get("T56_D_COUNTS")
    if path:
        with open(path, "w") as f:
            f.write(repr(getattr(config, "_t56_counts", None)) + "\n")
