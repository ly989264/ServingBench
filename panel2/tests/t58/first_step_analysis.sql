-- 第一步开销分析（只读，不上看板）：一个 D 实例、一个时间窗里的请求，第一步一行、第二步起一行（每步）。
-- 6 段：prepare_inputs、forward、lm_head、sampling、draft、bookkeeping；最后一列第一步是 prefill_time，第二步起是每步的
-- decode_time（decode_time ÷ 第二步起的步数），都是 vLLM 口径。
-- 填两处：<D 实例>（例如 D0）、<分钟>。只用面板二已有的列和 5 条不上看板的规则（step_device_span、graph_full、
-- graph_piecewise、graph_none、bookkeeping），段定义 glm53。
-- 时长单位 ms；6 段、span 都是设备时长，只在 D 的 tp_rank 0 有。第一步取各请求第一步的值，第二步起取各请求
-- （总和 − 第一步）÷ 第二步起的步数，再取中位数。某一段这个请求的第一步没有值（规则的 first_ns 为 0）时，这个请求
-- 不进这一段的中位数。6 段之和、span、空隙只算可比的请求：第一步是 6 段和 span 都有值的请求，第二步起是 6 段和 span
-- 第二步起的步数都相同的请求（有的步缺设备值时，各段的平均不是同一批步，相减没有意义）；「可比请求数」是这批请求的个数。
-- T58（v3.3-extra）：按每个请求 D 侧的粒度（core_summary[request] 的 diag_level，没有按 3）。低于 3 级时不写
-- bookkeeping 这一段的值，那对设备事件照记（用户选的做法乙），所以 step_device_span 各级含义相同；这时「6 段之和」
-- 只有 5 段（不含 bookkeeping），「空隙」里含 bookkeeping 的时间，可比请求也不看 bookkeeping 的步数。最后一列
-- 「D 侧粒度」列出这批请求的粒度；不止一种时，缩短时间窗，不跨换粒度的那次重启。
WITH per AS (
    SELECT request_id,
        max(acc__prepare_inputs__ns) AS pi_t, nullIf(max(acc__prepare_inputs__first_ns), 0) AS pi_f, max(acc__prepare_inputs__steps) AS pi_n,
        max(acc__forward__ns) AS fw_t, nullIf(max(acc__forward__first_ns), 0) AS fw_f, max(acc__forward__steps) AS fw_n,
        max(acc__lm_head__ns) AS lh_t, nullIf(max(acc__lm_head__first_ns), 0) AS lh_f, max(acc__lm_head__steps) AS lh_n,
        max(acc__sampling__ns) AS sp_t, nullIf(max(acc__sampling__first_ns), 0) AS sp_f, max(acc__sampling__steps) AS sp_n,
        max(acc__draft__ns) AS dr_t, nullIf(max(acc__draft__first_ns), 0) AS dr_f, max(acc__draft__steps) AS dr_n,
        max(acc__bookkeeping__ns) AS bk_t, nullIf(max(acc__bookkeeping__first_ns), 0) AS bk_f, max(acc__bookkeeping__steps) AS bk_n,
        max(acc__step_device_span__ns) AS sd_t, nullIf(max(acc__step_device_span__first_ns), 0) AS sd_f, max(acc__step_device_span__steps) AS sd_n,
        max(acc__graph_full__steps) AS gf_n, max(acc__graph_full__first_ns) AS gf_f,
        max(acc__graph_piecewise__steps) AS gp_n, max(acc__graph_piecewise__first_ns) AS gp_f,
        max(acc__graph_none__steps) AS gn_n, max(acc__graph_none__first_ns) AS gn_f,
        maxIf(first_step_vllm_ms, proc = 'api') AS prefill_ms, maxIf(rest_vllm_rest_ms, proc = 'api') AS decode_ms
    FROM vllm_pd.phase_line_cur
    WHERE role = 'decode' AND instance = '<D 实例>' AND ts > now() - INTERVAL <分钟> MINUTE /*请求*/
    GROUP BY request_id
),
lvl AS (
    SELECT x AS request_id, toUInt8(ifNull(min(JSONExtract(raw, 'diag_level', 'Nullable(UInt8)')), 3)) AS dlv
    FROM vllm_pd.diag_event
    WHERE event = 'core_summary[request]' AND side = 'decode' AND instance = '<D 实例>' AND ts > now() - INTERVAL <分钟> MINUTE - INTERVAL 10 MINUTE
    GROUP BY x
),
r AS (
    SELECT *,
        -- 第二步起每步的值：（总和 − 第一步）÷（步数 − 第一步那一步）
        if(pi_n - (pi_f > 0) > 0, (pi_t - ifNull(pi_f, 0)) / (pi_n - (pi_f > 0)), NULL) AS pi_r,
        if(fw_n - (fw_f > 0) > 0, (fw_t - ifNull(fw_f, 0)) / (fw_n - (fw_f > 0)), NULL) AS fw_r,
        if(lh_n - (lh_f > 0) > 0, (lh_t - ifNull(lh_f, 0)) / (lh_n - (lh_f > 0)), NULL) AS lh_r,
        if(sp_n - (sp_f > 0) > 0, (sp_t - ifNull(sp_f, 0)) / (sp_n - (sp_f > 0)), NULL) AS sp_r,
        if(dr_n - (dr_f > 0) > 0, (dr_t - ifNull(dr_f, 0)) / (dr_n - (dr_f > 0)), NULL) AS dr_r,
        if(bk_n - (bk_f > 0) > 0, (bk_t - ifNull(bk_f, 0)) / (bk_n - (bk_f > 0)), NULL) AS bk_r,
        if(sd_n - (sd_f > 0) > 0, (sd_t - ifNull(sd_f, 0)) / (sd_n - (sd_f > 0)), NULL) AS sd_r,
        if(lv >= 3, pi_f + fw_f + lh_f + sp_f + dr_f + bk_f, pi_f + fw_f + lh_f + sp_f + dr_f) AS six_f,
        if(lv >= 3, pi_r + fw_r + lh_r + sp_r + dr_r + bk_r, pi_r + fw_r + lh_r + sp_r + dr_r) AS six_r,
        pi_n - (pi_f > 0) AS pi_k,
        ifNull(pi_k > 0 AND pi_k = fw_n - (fw_f > 0) AND pi_k = lh_n - (lh_f > 0) AND pi_k = sp_n - (sp_f > 0)
            AND pi_k = dr_n - (dr_f > 0) AND (lv < 3 OR pi_k = bk_n - (bk_f > 0)) AND pi_k = sd_n - (sd_f > 0), 0) AS rest_ok,
        ifNull(gf_n, 0) + ifNull(gp_n, 0) + ifNull(gn_n, 0) AS steps_all
    FROM (SELECT per.*, if(lvl.dlv = 0, toUInt8(3), lvl.dlv) AS lv FROM per LEFT JOIN lvl ON lvl.request_id = per.request_id)
)
SELECT * FROM (
SELECT '第一步' AS "步",
    count() AS "请求数",
    round(quantileExact(0.5)(pi_f) / 1e6, 3) AS "prepare_inputs",
    round(quantileExact(0.5)(fw_f) / 1e6, 3) AS "forward",
    round(quantileExact(0.5)(lh_f) / 1e6, 3) AS "lm_head",
    round(quantileExact(0.5)(sp_f) / 1e6, 3) AS "sampling",
    round(quantileExact(0.5)(dr_f) / 1e6, 3) AS "draft",
    round(quantileExact(0.5)(bk_f) / 1e6, 3) AS "bookkeeping",
    countIf(isNotNull(six_f) AND isNotNull(sd_f)) AS "可比请求数",
    round(quantileExactIf(0.5)(six_f, isNotNull(sd_f)) / 1e6, 3) AS "6 段之和",
    round(quantileExactIf(0.5)(sd_f, isNotNull(six_f)) / 1e6, 3) AS "step_device_span",
    round(quantileExactIf(0.5)(sd_f - six_f, isNotNull(sd_f) AND isNotNull(six_f)) / 1e6, 3) AS "空隙（step_device_span − 6 段之和）",
    countIf(gf_f > 0) AS "FULL 步数",
    countIf(gp_f > 0) AS "PIECEWISE 步数",
    countIf(gn_f > 0) AS "NONE 步数",
    round(quantileExact(0.5)(prefill_ms), 3) AS "prefill_time / decode_time (vLLM)",
    arrayStringConcat(arrayMap(x -> toString(x), arraySort(groupUniqArray(lv))), '、') AS "D 侧粒度"
FROM r
UNION ALL
SELECT '第二步起（每步）' AS "步",
    count() AS "请求数",
    round(quantileExact(0.5)(pi_r) / 1e6, 3),
    round(quantileExact(0.5)(fw_r) / 1e6, 3),
    round(quantileExact(0.5)(lh_r) / 1e6, 3),
    round(quantileExact(0.5)(sp_r) / 1e6, 3),
    round(quantileExact(0.5)(dr_r) / 1e6, 3),
    round(quantileExact(0.5)(bk_r) / 1e6, 3),
    countIf(rest_ok),
    round(quantileExactIf(0.5)(six_r, rest_ok) / 1e6, 3),
    round(quantileExactIf(0.5)(sd_r, rest_ok) / 1e6, 3),
    round(quantileExactIf(0.5)(sd_r - six_r, rest_ok) / 1e6, 3),
    toUInt64(sum(ifNull(gf_n, 0) - (gf_f > 0))),
    toUInt64(sum(ifNull(gp_n, 0) - (gp_f > 0))),
    toUInt64(sum(ifNull(gn_n, 0) - (gn_f > 0))),
    round(quantileExactIf(0.5)(decode_ms / (steps_all - 1), steps_all > 1), 3),
    arrayStringConcat(arrayMap(x -> toString(x), arraySort(groupUniqArray(lv))), '、')
FROM r
)
ORDER BY "步" != '第一步'
