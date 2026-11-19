# Provisional (harness only): the kv.load_submit / kv.save_wait spans of patch 01's mixin hunk, on v0.30.0.
EDITS = [
 ("from vllm.forward_context import get_forward_context, set_forward_context\n",
  "from vllm.forward_context import get_forward_context, set_forward_context\nfrom vllm.v1.metrics import pd_diagnostics as diag\n", 1),
 ("""        start_after_forward = not scheduler_output.has_sync_kv_loads
        if not start_after_forward:
            kv_connector.start_load_kv(get_forward_context())
        try:
            yield output
        finally:
            if start_after_forward:
                kv_connector.start_load_kv(get_forward_context())
            if wait_for_save and not defer_finalize:
                kv_connector.wait_for_save()
""", """        start_after_forward = not scheduler_output.has_sync_kv_loads
        if not start_after_forward:
            with diag.span("kv.load_submit", completion="submission_return_not_transfer_completion"):
                kv_connector.start_load_kv(get_forward_context())
        try:
            yield output
        finally:
            if start_after_forward:
                with diag.span("kv.load_submit", completion="submission_return_not_transfer_completion"):
                    kv_connector.start_load_kv(get_forward_context())
            if wait_for_save and not defer_finalize:
                with diag.span("kv.save_wait", deferred=False):
                    kv_connector.wait_for_save()
""", 1),
]
