"""pytest plugin: a stub scipy for the vLLM test image, which has none.

tests/ut/conftest.py runs vllm_ascend.utils.adapt_patch(), whose imports reach vllm_ascend/attention/indexer.py and its
module-level `import scipy.linalg`. The scheduler tests never call scipy. numba (in the image) rejects a stub scipy,
so numba is imported first, as the joint check does.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from unittest.mock import MagicMock

try:
    import numba  # noqa: F401
except Exception:
    pass


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        module = MagicMock(name=spec.name)
        module.__spec__ = spec
        module.__path__ = []
        module.__name__ = spec.name
        module.__version__ = "0.0.0"
        return module

    def exec_module(self, module):
        pass


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "scipy":
            return importlib.machinery.ModuleSpec(name, _Loader(), is_package=True)
        return None


if importlib.util.find_spec("scipy") is None:
    sys.meta_path.insert(0, _Finder())
