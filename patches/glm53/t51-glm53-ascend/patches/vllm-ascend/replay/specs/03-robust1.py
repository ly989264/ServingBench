EDITS = [
 ("            pd_execution_id=scheduler_output.pd_execution_id,\n        )\n        # Immediate block free",
  "            pd_execution_id=getattr(scheduler_output, \"pd_execution_id\", None),\n        )\n        # Immediate block free", 1),
]
