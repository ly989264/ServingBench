# Stubs for the NPU-only packages, then the import of guide A step 4.3 with its time and peak memory (ru_maxrss).
import importlib.abc, importlib.machinery, importlib.util, resource, sys, time
from unittest.mock import MagicMock
STUB = [n for n in ("torch_npu", "torchair", "mooncake", "acl", "scipy") if importlib.util.find_spec(n) is None]
class L(importlib.abc.Loader):
    def create_module(self, spec):
        m = MagicMock(name=spec.name); m.__spec__ = spec; m.__path__ = []; m.__name__ = spec.name; m.__version__ = "0.0.0"; return m
    def exec_module(self, m): pass
class F(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        return importlib.machinery.ModuleSpec(name, L(), is_package=True) if name.split(".")[0] in STUB else None
sys.meta_path.insert(0, F())
t = time.time()
import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m
print(hasattr(m.KVCacheRecvingThread, "_transfer_batch"), "%.1f s" % (time.time() - t), "%d MB" % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024), "stubs=" + ",".join(STUB), "modules=%d" % len(sys.modules))
