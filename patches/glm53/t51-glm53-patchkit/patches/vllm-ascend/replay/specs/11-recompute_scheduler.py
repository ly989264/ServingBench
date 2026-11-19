# Count-checked edits on vllm_ascend/core/recompute_scheduler.py (vllm-ascend d4ad49796), applied by ../sub.py (replay.sh).
# RecomputeScheduler keeps its own copy of vLLM's schedule() body. With the vLLM port (patch 01, v0.30.0), the
# [PD_DIAG] scheduler record is opened by vLLM's Scheduler.schedule() around Scheduler._schedule(); this copy never
# reached it. The edits: the body becomes _schedule(); RecomputeScheduler.schedule() calls vLLM's own
# Scheduler.schedule() (found in the MRO, past BalanceScheduler); the five points 01 puts inside vLLM's body are
# added at the same places in this copy, with the same code.

EDITS = [
    # import, as in vLLM's scheduler.py
    (
        "from vllm.v1.outputs import ModelRunnerOutput\n",
        "from vllm.v1.metrics import pd_diagnostics as diag\nfrom vllm.v1.outputs import ModelRunnerOutput\n",
        1,
    ),
    # the body becomes _schedule(); schedule() stays defined here and goes through vLLM's wrapper
    (
        "    def schedule(self, throttle_prefills: bool = False) -> SchedulerOutput:\n"
        "        self.current_step += 1\n",
        "    def schedule(self, throttle_prefills: bool = False) -> SchedulerOutput:\n"
        "        # vLLM's own Scheduler.schedule() writes the [PD_DIAG] scheduler record around self._schedule(). Not\n"
        "        # super(): with BalanceScheduler as the base it would also run BalanceScheduler.schedule and the hooks on\n"
        "        # it, which this class never ran. Not imported by name: that name may be BalanceScheduler by now.\n"
        "        for cls in type(self).__mro__:\n"
        '            if cls.__module__ == "vllm.v1.core.sched.scheduler" and cls.__qualname__ == "Scheduler":\n'
        "                return cls.schedule(self, throttle_prefills)\n"
        '        raise RuntimeError("vLLM\'s Scheduler is not a base of this scheduler")\n'
        "\n"
        "    def _schedule(self, throttle_prefills: bool = False) -> SchedulerOutput:\n"
        "        self.current_step += 1\n",
        1,
    ),
    # point 1: a waiting request becomes a scheduling candidate
    (
        "                request = request_queue.peek_request()\n"
        "                request_id = request.request_id\n",
        "                request = request_queue.peek_request()\n"
        '                request.pd_queue_transition("candidate")\n'
        "                request_id = request.request_id\n",
        1,
    ),
    # point 2: the external (KV connector) lookup range
    (
        "                        ext_tokens, load_kv_async = self.connector.get_num_new_matched_tokens(\n"
        "                            request, block_aligned_local\n"
        "                        )\n",
        "                        lookup_start = time.monotonic_ns()\n"
        '                        lookup_status = "ok"\n'
        "                        lookup_error = None\n"
        "                        try:\n"
        "                            ext_tokens, load_kv_async = self.connector.get_num_new_matched_tokens(\n"
        "                                request, block_aligned_local\n"
        "                            )\n"
        "                        except BaseException as exc:\n"
        "                            lookup_status = (\n"
        '                                "cancelled"\n'
        "                                if isinstance(exc, (KeyboardInterrupt, GeneratorExit))\n"
        '                                or type(exc).__name__ == "CancelledError"\n'
        '                                else "error"\n'
        "                            )\n"
        "                            lookup_error = type(exc).__name__\n"
        "                            raise\n"
        "                        finally:\n"
        "                            lookup_end = time.monotonic_ns()\n"
        "                            self._pd_external_lookup_ns += lookup_end - lookup_start\n"
        "                            self._pd_external_lookup_calls += 1\n"
        "                            diag.current_batch().add_stage(\n"
        '                                "lookup",\n'
        "                                start_ns=lookup_start,\n"
        "                                end_ns=lookup_end,\n"
        "                                status=lookup_status,\n"
        "                                error_type=lookup_error,\n"
        "                            )\n",
        1,
    ),
    # point 3: a waiting request is scheduled in this step
    (
        "                    has_sync_kv_loads = True\n"
        "                if self.log_stats:\n"
        "                    request.record_event(EngineCoreEventType.SCHEDULED, scheduled_timestamp)\n",
        "                    has_sync_kv_loads = True\n"
        '                request.pd_execution_id = diag.current().get("execution_id")\n'
        "                if self.log_stats:\n"
        "                    request.record_event(EngineCoreEventType.SCHEDULED, scheduled_timestamp)\n",
        1,
    ),
    # point 4: the step's execution id on the scheduler output
    (
        "        scheduler_output = RecomputeSchedulerOutput(\n"
        "            scheduled_new_reqs=new_reqs_data,\n",
        "        scheduler_output = RecomputeSchedulerOutput(\n"
        '            pd_execution_id=diag.current().get("execution_id"),\n'
        "            scheduled_new_reqs=new_reqs_data,\n",
        1,
    ),
    # point 5: per-request counters and the step's members, before _update_after_schedule
    (
        "            self.sched_step_seq += 1\n"
        "\n"
        '        with record_function_or_nullcontext("schedule: update_after_schedule"):\n',
        "            self.sched_step_seq += 1\n"
        "\n"
        "        members = []\n"
        "        for request_id, scheduled_tokens in num_scheduled_tokens.items():\n"
        "            request = self.requests[request_id]\n"
        "            request.pd_execution_id = scheduler_output.pd_execution_id\n"
        "            request.pd_executions += 1\n"
        "            if scheduled_tokens > 0 and request.num_computed_tokens >= request.num_prompt_tokens:\n"
        "                request.pd_decode_steps += 1\n"
        "            members.append(\n"
        "                {\n"
        '                    "request_id": request_id,\n'
        '                    "observation_id": request.pd_identity.get("observation_id"),\n'
        '                    "engine_id": self._pd_engine_id,\n'
        '                    "scheduled_tokens": scheduled_tokens,\n'
        '                    "num_computed_tokens_before": request.num_computed_tokens,\n'
        '                    "num_output_tokens": request.num_output_tokens,\n'
        '                    "num_prompt_tokens": request.num_prompt_tokens,\n'
        '                    "num_tokens": request.num_tokens,\n'
        '                    "is_prefill_chunk": (\n'
        "                        request.num_computed_tokens + scheduled_tokens\n"
        "                        < request.num_tokens + request.num_output_placeholders\n"
        "                    ),\n"
        '                    "prefill_tokens_remaining_before": max(\n'
        "                        request.num_prompt_tokens - request.num_computed_tokens, 0\n"
        "                    ),\n"
        '                    "num_spec_tokens": len(scheduled_spec_decode_tokens.get(request_id, ())),\n'
        '                    "execution_participation": request.pd_executions,\n'
        "                }\n"
        "            )\n"
        "        diag.current_batch().update(\n"
        "            request_ids=list(num_scheduled_tokens),\n"
        "            members=members,\n"
        "            no_forward=total_num_scheduled_tokens == 0,\n"
        "            finished_request_ids=list(scheduler_output.finished_req_ids),\n"
        "            preempted_request_ids=[request.request_id for request in preempted_reqs],\n"
        "        )\n"
        "\n"
        '        with record_function_or_nullcontext("schedule: update_after_schedule"):\n',
        1,
    ),
]
