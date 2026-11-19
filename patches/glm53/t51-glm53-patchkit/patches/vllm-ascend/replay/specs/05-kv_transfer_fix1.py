EDITS = [
 ("""                remaining = self.get_stored_request_count(req_id)
                if remaining is not None:
                    pool_diag.add(completed_store_jobs=1)
                    pool_diag.completion(
                        request_id=req_id,
                        execution_id=req_meta.pd_execution_id,
                        store_job_id=getattr(req_meta, "store_job_id", None),
                        tp_rank=self.tp_rank,
                        remaining_jobs=remaining,
                    )
                if remaining == 0:
""", """                remaining = self.get_stored_request_count(req_id)
                if remaining == 0:
""", 1),
 ("""            if remaining is not None:
                    pool_diag.add(completed_store_jobs=1)
                    pool_diag.completion(
                        request_id=req_id,
                        execution_id=req_meta.pd_execution_id,
                        store_job_id=getattr(req_meta, "store_job_id", None),
                        tp_rank=self.tp_rank,
                        remaining_jobs=remaining,
                    )
""", """            if remaining is not None:
                pool_diag.add(completed_store_jobs=1)
                pool_diag.completion(
                    request_id=req_id,
                    execution_id=req_meta.pd_execution_id,
                    store_job_id=getattr(req_meta, "store_job_id", None),
                    tp_rank=self.tp_rank,
                    remaining_jobs=remaining,
                )
""", 1),
]
