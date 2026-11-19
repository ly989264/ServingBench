"""pytest plugin: stub third-party packages that are not installed on the lab Mac (T51_STUB=a,b).

Only for importing vllm / vllm_ascend modules that pull them in at import time; a test that
actually calls into a stubbed package is not meaningful.
"""
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
from unittest.mock import MagicMock

_ROOTS = tuple(n for n in os.environ.get("T51_STUB", "").split(",") if n and importlib.util.find_spec(n) is None)


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = MagicMock(name=spec.name)
        module.__spec__ = spec
        module.__path__ = []
        module.__name__ = spec.name
        return module

    def exec_module(self, module):
        pass


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _ROOTS:
            return importlib.machinery.ModuleSpec(name, _StubLoader(), is_package=True)
        return None


if _ROOTS:
    sys.meta_path.insert(0, _StubFinder())
