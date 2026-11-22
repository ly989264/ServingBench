WITH '<D 实例>' AS inst, toDateTime64('<开始时刻>', 3, 'UTC') AS t0,
r AS (
 SELECT request_id, min(ts) AS t_first,
  max(acc__prepare_inputs__first_ns) AS pi1, max(acc__prepare_inputs__ns) AS pi, max(acc__forward__first_ns) AS fw1, max(acc__forward__ns) AS fw,
  max(acc__lm_head__first_ns) AS lm1, max(acc__lm_head__ns) AS lm, max(acc__sampling__first_ns) AS sa1, max(acc__sampling__ns) AS sa,
  max(acc__bookkeeping__first_ns) AS bk1, max(acc__bookkeeping__ns) AS bk, max(acc__draft__first_ns) AS dr1, max(acc__draft__ns) AS dr,
  max(acc__step_device_span__first_ns) AS sp1, max(acc__step_device_span__ns) AS sp,
  max(acc__graph_full__first_ns) AS gf1, max(acc__graph_piecewise__first_ns) AS gp1, max(acc__graph_none__first_ns) AS gn1,
  max(acc__graph_full__steps) AS gfs, max(acc__graph_piecewise__steps) AS gps, max(acc__graph_none__steps) AS gns,
  max(start_load_kv_ms) AS env, max(start_load_kv__receive_queue_ms) AS rq, max(start_load_kv__prepare_read_ms) AS rp,
  max(start_load_kv__wait_for_transfer_ms) AS wft, max(kv_ready_wait_ms) AS krw, max(schedule__queue_time_ms) AS qt,
  max(kv_wait_vllm_ms) AS kvv, max(first_step_vllm_ms) AS fsv,
  argMinIf(first_exec, first_mono_ns, proc = 'engine' AND part != '' AND first_exec != '') AS fexec
 FROM vllm_pd.phase_line_cur WHERE role = 'decode' AND instance = inst AND ts >= t0 - INTERVAL 1 HOUR
 GROUP BY request_id HAVING t_first >= t0),
k AS (
 SELECT x, any(JSONExtractString(raw, 'stages', 'kv_ready_wait', 'ranges', 1, 'scheduled_execution_id')) AS sexec
 FROM vllm_pd.diag_event WHERE event = 'core_summary[request]' AND side = 'decode' AND instance = inst AND ts >= t0 - INTERVAL 1 HOUR AND JSONHas(raw, 'stages', 'kv_ready_wait')
 GROUP BY x),
j AS (SELECT r.*, k.sexec AS sexec, k.x != '' AS has_krw, r.rq IS NOT NULL AS from_p,
  coalesce(pi1, 0) + coalesce(fw1, 0) + coalesce(lm1, 0) + coalesce(sa1, 0) + coalesce(bk1, 0) + coalesce(dr1, 0) AS six1,
  coalesce(pi, 0) + coalesce(fw, 0) + coalesce(lm, 0) + coalesce(sa, 0) + coalesce(bk, 0) + coalesce(dr, 0) AS six
  FROM r LEFT JOIN k ON k.x = r.request_id),
fb AS (
 SELECT sum(f1 - f0) AS miss, sum(a1 - a0) AS recs FROM (
  SELECT pid,
   argMax(acc_fallback['forward'] + acc_fallback['sampling'] + acc_fallback['prepare_inputs'] + acc_fallback['lm_head'] + acc_fallback['draft'] + acc_fallback['bookkeeping'], ts) AS f1,
   argMin(acc_fallback['forward'] + acc_fallback['sampling'] + acc_fallback['prepare_inputs'] + acc_fallback['lm_head'] + acc_fallback['draft'] + acc_fallback['bookkeeping'], ts) AS f0,
   argMax(accumulated, ts) AS a1, argMin(accumulated, ts) AS a0
  FROM vllm_pd.filter_selfcheck WHERE instance = inst AND role = 'worker' AND ts >= t0 GROUP BY pid)),
c AS (
 SELECT 1 AS o, 'span_first' AS item, countIf(sp1 > 0) AS n, countIf(sp1 > 0 AND six1 > sp1) AS bad, '' AS note FROM j
 UNION ALL SELECT 2, 'span_rest', countIf(sp > sp1), countIf(sp > sp1 AND six - six1 > sp - sp1), '' FROM j
 UNION ALL SELECT 3, 'first_step_le_vllm', countIf(fsv IS NOT NULL AND fw1 IS NOT NULL), countIf(fsv IS NOT NULL AND fw1 IS NOT NULL AND six1 / 1e6 > fsv), '' FROM j
 UNION ALL SELECT 4, 'envelope', countIf(from_p AND env IS NOT NULL), countIf(from_p AND env IS NOT NULL AND env < greatest(coalesce(wft, 0), coalesce(rq, 0), coalesce(rp, 0))), 'no envelope (not marked done): ' || toString(countIf(from_p AND env IS NULL)) FROM j
 UNION ALL SELECT 5, 'kv_wait_vllm_ge_sum', countIf(from_p AND kvv IS NOT NULL AND qt IS NOT NULL AND env IS NOT NULL AND krw IS NOT NULL),
  countIf(from_p AND kvv IS NOT NULL AND qt IS NOT NULL AND env IS NOT NULL AND krw IS NOT NULL AND kvv < qt + env + krw), '' FROM j
 UNION ALL SELECT 6, 'kv_ready_wait_present', countIf(from_p), countIf(from_p AND NOT has_krw), '' FROM j
 UNION ALL SELECT 7, 'kv_ready_wait_same_step', countIf(has_krw), countIf(has_krw AND sexec != fexec), '' FROM j
 UNION ALL SELECT 8, 'device_missing', toUInt64(recs), toUInt64(miss), 'per 100 records: ' || toString(round(100 * miss / greatest(recs, 1), 2)) FROM fb
 UNION ALL SELECT 9, 'graph_rest_none', toUInt64(sum(coalesce(gfs, 0) + coalesce(gps, 0) + coalesce(gns, 0)) - countIf(gf1 > 0 OR gp1 > 0 OR gn1 > 0)), toUInt64(sum(coalesce(gns, 0)) - countIf(gn1 > 0)),
  'first step: FULL ' || toString(countIf(gf1 > 0)) || ', PIECEWISE ' || toString(countIf(gp1 > 0)) || ', NONE ' || toString(countIf(gn1 > 0)) FROM j),
 res AS (SELECT o, item, n, bad, if(n = 0, 'NO_DATA', if(o IN (8, 9), if(bad * 100 <= n, 'PASS', 'FAIL'), if(bad = 0, 'PASS', 'FAIL'))) AS result, note FROM c)
SELECT item, n, bad, result, note FROM (
 SELECT o, item, n, bad, result, note FROM res
 UNION ALL SELECT 99, 'RESULT', toUInt64(0), toUInt64(countIf(result = 'FAIL')), if(countIf(result = 'FAIL') > 0, 'FAIL', if(countIf(result = 'NO_DATA') > 0, 'NO_DATA', 'PASS')), '' FROM res)
ORDER BY o
FORMAT PrettyCompactMonoBlock
