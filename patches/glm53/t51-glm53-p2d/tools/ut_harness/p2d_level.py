"""pytest plugin for lab runs: with P2D_TEST_DIAG_LEVEL=<n>, pd_diagnostics.diag_level() returns int(n) in every test.

Load with `-p p2d_level` (the harness directory is on PYTHONPATH). Without the variable it does nothing, so the
level comes from the vLLM tree under test (no diag_level there: 3). A test that sets the level itself wins.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _p2d_test_diag_level(monkeypatch):
    value = os.environ.get("P2D_TEST_DIAG_LEVEL")
    if value:
        from vllm.v1.metrics import pd_diagnostics

        monkeypatch.setattr(pd_diagnostics, "diag_level", lambda: int(value), raising=False)
    yield
