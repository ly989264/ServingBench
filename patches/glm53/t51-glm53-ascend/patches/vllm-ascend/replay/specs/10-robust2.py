EDITS = [
 ("            snapshot = pool_diag.capture(None, \"save_wait\", self.tp_rank, queued=False)\n",
  "            snapshot = pool_diag.capture(None, \"save_wait\", getattr(self, \"tp_rank\", None), queued=False)\n", 1),
 ("""                self._diagnostic_deferred_save_snapshot = pool_diag.capture(
                    connector_metadata, "save_wait", self.tp_rank, queued=False
                )
            else:
                with pool_diag.processing(pool_diag.capture(connector_metadata, "save_wait", self.tp_rank, queued=False)):
""", """                self._diagnostic_deferred_save_snapshot = pool_diag.capture(
                    connector_metadata, "save_wait", getattr(self, "tp_rank", None), queued=False
                )
            else:
                with pool_diag.processing(
                    pool_diag.capture(connector_metadata, "save_wait", getattr(self, "tp_rank", None), queued=False)
                ):
""", 1),
]
