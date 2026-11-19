# A2 接口：runner 接管一步 worker_summary 的写出（T51 C14）

给 vLLM v0.30.0 移植版（`vllm/v1/worker/worker_base.py`）。默认关闭：只要没有 runner 调用 `pd_claim_finish()`，记录照旧在 `execute_model`、`sample_tokens` 返回时写出，内容和现在完全相同。`pd_diagnostics.py` 不改。

参考实现：`../joint/provisional-vllm/worker_base_a2_spec.py`（在补丁 01 的 `worker_base.py` 上做的 4 处替换，可以照抄，也可以按自己的写法实现，只要行为和下面一致）。vllm-ascend 侧按这里的名字和语义调用，联合检查也按它测。

## 名字和签名

```python
# vllm/v1/worker/worker_base.py

class PdFinishClaim:
    def set_stage_fields(self, stage: str, **fields: Any) -> bool: ...
    def release(self) -> None: ...

def pd_claim_finish() -> PdFinishClaim | None: ...
def pd_release_claims() -> None: ...
```

## 语义

| 名字 | 行为 |
|---|---|
| `pd_claim_finish()` | 在被 `pd_execution` 或 `pd_sample` 包住的方法里、同一线程上调用。效果：这个方法正常返回时，包装器**不写**这一步这个阶段的 `worker_summary`，留给返回的 claim 写。当前没有打开的 `worker_summary`（dummy run、图捕获、没有诊断）时返回 `None`。同一条记录重复调用，返回同一个 claim |
| 方法抛异常 | 包装器**立刻写出**，不推迟：原样用和现在相同的 `status`（`error` 或 `cancelled`）和 `error_type` 调 `finish()`；只要状态不是 `ok` 就不交给 claim。此后这个 claim 的 `release()` 什么都不做，`set_stage_fields()` 返回 `False`。原因：出错的那一步不会产生异步输出，推迟了就只能等兜底或退出时才写出 |
| `PdFinishClaim.set_stage_fields(stage, **fields)` | 把字段加进还没写出的记录的 `stages[stage]`。`stage` 不存在或记录已写出时返回 `False`。线程安全（用 `Batch` 自己的锁） |
| `PdFinishClaim.release()` | 可重复调用，任何线程都能调。**方法还没返回时**调用：取消推迟，记录在方法返回时照常写出。**方法已返回后**调用：立刻写出，状态用包装器当时定下的（正常返回为 `ok`，`error_type` 为 `null`） |
| `pd_release_claims()` | 写出所有方法已返回、但还没 release 的记录。`worker_base` 用 `atexit` 注册它，进程退出时不丢记录 |

## 不变的地方

- 记录的字段、取值和现在相同，只多 runner 自己加的字段（C14 只有 `device_duration_ns`），写出时刻推后。
- `finish()` 只被调用一次（`Batch.finish` 本身也有幂等标志）。
- 被包住方法的返回值和异常都不变。
- V2 runner、vLLM 自己的 GPU runner 不调用 `pd_claim_finish()`，行为不变。
