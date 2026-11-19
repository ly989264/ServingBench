"""Environment shim for running the patch-01 unit tests on a CPU-only box.

The repo-wide autouse cleanup fixture calls accelerator memory APIs after every
test, which raise on a host without an accelerator. Marking every collected
test with the repo's own ``skip_global_cleanup`` marker turns that teardown off.
No test body or assertion is changed.
"""
import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.skip_global_cleanup)


def pytest_sessionstart(session):
    _subst_tokenizer()


def pytest_collection_finish(session):
    _subst_tokenizer()


def _subst_tokenizer():
    """T51_SUBST_TOKENIZER replaces the gated tokenizer of tests/v1/engine.

    The original (meta-llama/Llama-3.2-1B) needs an HF token we do not have.
    Expected strings in those tests are computed from the tokenizer itself.
    """
    import os
    import sys

    sub = os.environ.get("T51_SUBST_TOKENIZER")
    if not sub:
        return
    for name in ("tests.v1.engine.utils", "tests.v1.engine.conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "TOKENIZER_NAME"):
            module.TOKENIZER_NAME = sub
