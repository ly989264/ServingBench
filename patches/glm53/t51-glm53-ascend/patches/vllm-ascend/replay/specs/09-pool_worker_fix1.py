EDITS = [
 ("""        snapshot = self._diagnostic_deferred_save_snapshot
        self._diagnostic_deferred_save_snapshot = None
""", """        snapshot = getattr(self, "_diagnostic_deferred_save_snapshot", None)
        self._diagnostic_deferred_save_snapshot = None
        if snapshot is None:
            snapshot = pool_diag.capture(None, "save_wait", self.tp_rank, queued=False)
""", 1),
]
