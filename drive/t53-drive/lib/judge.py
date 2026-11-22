#!/usr/bin/env python3
"""t53drive, monitor side: judge one machine's output of one stage. Standard library only; python3 -I.

  judge.py <stage> <log file> <kv file>
The expectations come from the environment (t53drive.sh exports them): ROLE, PORTS, NAME, D_TOTAL, MOONCAKE, and the
EXP_* values of expect.sh. Prints one line, "OK <summary>" or "FAIL <reason>" (Chinese, no addresses), and appends
the values to keep to the kv file as key=value lines.
"""
import os
import re
import sys

E = os.environ


def sections(text):
    out, cur = {}, None
    for line in text.split("\n"):
        if line.startswith("== "):
            cur = line[3:].strip()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return out


def kv_of(lines):
    d = {}
    for l in lines:
        m = re.match(r"^([A-Za-z0-9_]+)=(.*)$", l)
        if m:
            d[m.group(1)] = m.group(2)
    return d


def keep_of(text):
    d = {}
    for l in text.split("\n"):
        if l.startswith("K ") and "=" in l:
            k, _, v = l[2:].partition("=")
            d[k] = v
    return d


def load_kv(path):
    d = {}
    if os.path.exists(path):
        for l in open(path):
            k, _, v = l.rstrip("\n").partition("=")
            if k:
                d[k] = v
    return d


class Fail(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Fail(msg)


def ports_list():
    return [int(x) for x in E["PORTS"].split(",")]


def det_lines(lines):
    d = {"inst": []}
    for l in lines:
        for k in ("vllm=", "ascend=", "site_patches ", "strict ", "verdict ", "run ", "mooncake="):
            if l.startswith(k):
                d[k.strip(" =")] = l
        if l.startswith("inst[run"):
            d["inst"].append(l)
        if "baseline_token=" in l:
            d["baseline_token"] = l.split("baseline_token=", 1)[1].strip()
        if l.strip() in ("BLOCKED", "REFUSED") or l.startswith("BLOCKED") or l.startswith("REFUSED"):
            d["blocked"] = l.strip()
        if l.startswith("BASELINE_DIFF"):
            d["baseline_diff"] = l.strip()
        if l.strip() == "baseline=same":
            d["baseline_same"] = True
        m = re.match(r"detect_exit=(\d+)", l)
        if m:
            d["exit"] = int(m.group(1))
    return d


def field(line, key):
    m = re.search(r"(?:^| )%s=(\S+)" % re.escape(key), line or "")
    return m.group(1) if m else None


def expand_ports(s):
    out = []
    for part in (s or "").split(","):
        if re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-"))
            out += list(range(a, b + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def judge_detect(d, phase, keep, old=None):
    need("blocked" not in d, "检测脚本只打印了 %s" % d.get("blocked"))
    need(d.get("exit") == 0, "检测脚本退出码 %s" % d.get("exit"))
    v, a, r = d.get("vllm", ""), d.get("ascend", ""), d.get("run", "")
    need("from=site cands=1" in v and "from=site cands=1" in a, "vLLM 或 vllm-ascend 不是 site-packages 里唯一的一份（from=、cands=）")
    verdict = d.get("verdict", "")
    if phase == "pre":
        need(field(v, "state") == "v0.30.0", "vllm 状态是 %s，不是现场原样 v0.30.0" % field(v, "state"))
        need(field(v, "port") == "t51:0/25,t56:0/25,t58:0/25", "vllm 已经有补丁文件（port=%s）" % field(v, "port"))
        need(field(a, "port") == "t51:0/11,t56:0/11,t58:0/11", "vllm-ascend 已经有补丁文件（port=%s）" % field(a, "port"))
        need(re.search(r"verdict vllm=unpatched ascend=unpatched\(site_local=\d+\) p2d=unpatched", verdict),
             "检测结论不是三项 unpatched：%s" % verdict[8:90])
        need("p2d=not_replayed" in d.get("strict", ""), "p2d 已经重放过")
        need(field(r, "logcfg") == "unset", "启动环境里已经有面板二的日志配置（logcfg=%s）" % field(r, "logcfg"))
        mc = field(verdict, "mooncake")
        keep["mc_verdict"] = mc or "-"
        if E.get("MOONCAKE") == "replace":
            need(mc == E.get("EXP_MC_VERDICT_PRE", "not_replaced"), "Mooncake 检测是 %s：这次不能换库，清单改 MOONCAKE=skip" % mc)
        keep["baseline_token"] = d.get("baseline_token", "")
        need(keep["baseline_token"], "检测没有打印 baseline_token")
    else:
        need(re.search(r"verdict vllm=ok\(t58\) ascend=ok\(t58,site_local=\d+\) p2d=ok\(t58\)", verdict),
             "检测结论不是三项 ok(t58)：%s" % verdict[8:90])
        need(field(v, "state") == "v0.30.0+t58", "vllm 状态是 %s" % field(v, "state"))
        need("p2d=ok(t58,changes=29/29" in d.get("strict", ""), "p2d 不是 29/29")
        need(field(r, "logcfg") == "set", "vLLM 没有读到面板二的日志配置（logcfg=%s）：两行 export 没生效" % field(r, "logcfg"))
        need("baseline_diff" not in d, "运行模式和打补丁前不同：%s" % d.get("baseline_diff", "")[:80])
        need(d.get("baseline_same"), "检测没有打印 baseline=same")
        mc = field(verdict, "mooncake")
        if E.get("MC_SWAPPED") == "1":
            need(mc == "ok(v2)", "Mooncake 检测是 %s，不是 ok(v2)" % mc)
        else:   # the library was not swapped: the detector says the same as in stage ①
            need((mc or "-") == (old or {}).get("mc_verdict", "-"), "Mooncake 检测是 %s，① 是 %s" % (mc, (old or {}).get("mc_verdict", "-")))
    dl = r.split("diag_level=", 1)[1].split()[0] if "diag_level=" in r else None
    need(dl == "3(default)", "启动环境里设了 PD_DIAG_LEVEL（diag_level=%s），先去掉" % dl)
    # instances: the ports of this machine, the run mode
    ports, ranks = [], 0
    for l in d["inst"]:
        ports += expand_ports(field(l, "ports"))
        rk = field(l, "ranks")
        if rk and rk != "?":
            ranks += len(expand_ports(rk))
        kind = field(l, "kind")
        if E["ROLE"] == "D":
            need(kind == "D", "D 机器上检测出 kind=%s 的实例" % kind)
            for k, want in (("tp", "1"), ("dp", E["D_TOTAL"]), ("eager", "0"), ("async", "1")):
                need(field(l, k) == want, "D 的 %s=%s，按清单应为 %s" % (k, field(l, k), want))
        else:
            need(kind == "P", "P 机器上检测出 kind=%s 的实例" % kind)
            need(field(l, "tp") == E.get("EXP_P_TP", "16"), "P 的 tp=%s，应为 %s" % (field(l, "tp"), E.get("EXP_P_TP", "16")))
    need(sorted(ports) == sorted(ports_list()), "检测到的端口 %s 和清单 %s 不同" % (span(ports), span(ports_list())))
    if E["ROLE"] == "D":
        need(ranks == len(ports_list()), "D 的 DP rank 个数 %d 和端口个数 %d 不同" % (ranks, len(ports_list())))
    sp = d.get("site_patches", "")
    keep["site_patches"] = " ".join(sp.split()[1:])


def span(ps):
    ps = sorted(ps)
    if not ps:
        return "无"
    out, a, b = [], ps[0], ps[0]
    for x in ps[1:] + [None]:
        if x is not None and x == b + 1:
            b = x
            continue
        out.append(str(a) if a == b else "%d-%d" % (a, b))
        if x is not None:
            a = b = x
    return ",".join(out)


def j_pre(S, keep, old):
    t = S.get("t64", [])
    need(not any(l.startswith("t64_left=") for l in t), "先做 T64 现场升级（这台机器上还有旧面板二的 mock 在跑）")
    c = kv_of(S.get("container", []))
    need("container" in S and c.get("find") in ("found", "given"),
         "没找到 vLLM 容器（%s）：在清单第 4 列写容器名" % (c.get("find") or "?"))
    dv = S.get("devino", [])
    h = [l.split()[1] for l in dv if l.startswith("host ")]
    cc = [l.split("devino=")[-1] for l in dv if l.startswith("cont ")]
    need(h and cc and h[0] == cc[0], "容器里看不到宿主机的 /data/pd-diag（要重建容器加挂载，不在本脚本里）")
    kit = [l for l in S.get("kit", []) if l.strip() and l not in ("kit_sums_done", "mc_sums_done") and not l.startswith("kit_exit=")]
    need(not kit and "kit_exit=0" in S.get("kit", []), "补丁包拷进容器后 sha256 不对，或者解不开")
    p = kv_of(S.get("procs", []))
    need(int(p.get("vllm_procs", "0")) > 0, "容器里没有 vLLM 进程：① 要在集群照常运行时做")
    need(sorted(expand_ports(p.get("ports", ""))) == sorted(ports_list()),
         "容器里 vLLM 的端口 %s 和清单 %s 不同" % (p.get("ports", "无"), span(ports_list())))
    st = keep.get("start", "")
    need(st and st != "-" and ";" not in st, "启动文件找到 %s 个：请在清单第 5 列写启动文件" % (len(st.split(";")) if st and st != "-" else 0))
    need(keep.get("logs", "-") != "-", "找不到 vLLM 的日志文件（标准输出不是文件）")
    for k in ("PD_DIAG_LEVEL", "MC_LOG_DIR", "MC_PD_TIMING"):
        need(p.get("env_" + k) == "<unset>", "vLLM 的启动环境里设了 %s，先去掉" % k)
    keep["cmdline_sha16"] = p.get("cmdline_sha16", "")
    keep["purelib"] = p.get("purelib", "")
    keep["ids"] = p.get("ids", "")
    judge_detect(det_lines(S.get("detect", [])), "pre", keep)
    f = kv_of(S.get("files", []))
    need(f.get("before_vllm") == "23" and f.get("before_ascend") == "9" and f.get("before_bad") == "none",
         "要换的文件不是公开版本（%s）" % f.get("before_bad", "?")[:80])
    if E["ROLE"] == "D":
        need(f.get("unchanged_bad") == "none", "D 上 platform.py 或 utils.py 有本地改动（%s）" % f.get("unchanged_bad"))
    keep["unchanged_bad"] = f.get("unchanged_bad", "")
    need(f.get("connector_sha16") == E["EXP_CONN"], "现场的 mooncake_connector.py 和预期不同（%s）" % f.get("connector_sha16"))
    need(f.get("site_platform_patch_kv_load_failure_reason") == E["EXP_SITE_KV"],
         "本地补丁 patch_kv_load_failure_reason.py 和预期不同")
    pre = f.get("site_platform_patch_prefill_length_validation")
    need(pre == E["EXP_SITE_PREFILL"] or (E["ROLE"] == "D" and pre == "absent"), "本地补丁 patch_prefill_length_validation.py 和预期不同")
    keep["site_init"] = "%s %s" % (f.get("site___init__"), f.get("site_platform___init__"))
    keep["prefill_kwargs"] = f.get("prefill_kwargs", "")
    pl = S.get("p2d", [])
    need("p2d_check_exit=0" in pl and any("check ok: 29 of 29" in l for l in pl), "p2d 检查没有通过（29 处）")
    alt = sorted(re.match(r"replay_p2d: (C\d+) alternative", l).group(1) for l in pl if re.match(r"replay_p2d: C\d+ alternative", l))
    need(" ".join(alt) == E["EXP_P2D_ALT"], "p2d 用了备选锚点的编号 %s，和预期 %s 不同" % (" ".join(alt) or "无", E["EXP_P2D_ALT"]))
    need(E["EXP_P2D_EXTRA"] in pl, "p2d 补充核对 X1–X6 和预期不同")
    x = kv_of(S.get("exports", []))
    need(x.get("exports") in ("absent", "ok"), "启动文件里的 export：%s" % (x.get("exports") or "?"))
    keep["exports"] = x.get("exports")
    keep["insert"] = x.get("insert_before", "-")
    keep["anchor"] = x.get("anchor_sha8", "-")
    sp = kv_of(S.get("space", []))
    keep["tmpfs"] = sp.get("tmp_fstype", "?")
    need(float(sp.get("free_gb", "0")) >= float(E.get("MIN_FREE_GB", "50")), "节点目录所在盘只剩 %s GB（要 %s GB）" % (sp.get("free_gb"), E.get("MIN_FREE_GB", "50")))
    offs = []
    for l in S.get("logsizes", []):
        m = re.match(r"S size (\S+) (\d+)$", l)
        if m:
            offs.append("%s:%s" % (m.group(1), m.group(2)))
    keep["offsets"] = ";".join(offs)
    if E.get("MOONCAKE") == "replace":
        mp = S.get("mcpre", [])
        res = [l for l in mp if l.startswith("mc_pre result=")]
        r = res[-1].split("=", 1)[1] if res else "none"
        keep["mc_pre"] = r
        if r != "ok_replace":
            ok_exc = r == "STOP_build_differs" and any("sections_other=.comment:51/102" in l and re.search(r"actual=(\S+) .*predicted=\1", l) for l in mp) \
                and any("differing_families=none replacement_vs_site=-0/+3(expected -0/+3)" in l for l in mp) \
                and any("removed_names=0" in l for l in mp)
            need(ok_exc, "Mooncake 换库前核对是 %s：这次不能换库，清单改 MOONCAKE=skip" % r)
            keep["mc_pre"] = "comment_only"
    return "%d 个实例，启动文件第 %s 行前加 export" % (len(ports_list()), keep["insert"]) if keep["exports"] == "absent" else \
        "%d 个实例，export 已在" % len(ports_list())


def j_mcexc(S, keep):
    lines = S.get("mcexc", [])
    need("exc_sums_done" in lines and not any(l.strip() for l in lines[:lines.index("exc_sums_done")]),
         "例外核对的两条命令 sha256 不对")
    need(not any(l == "mc_pid=none" for l in lines), "容器里没有进程映射 Mooncake 库")
    for w in [w for w in E.get("EXP_MC_COMMENT", "").split("|") if w]:
        need(any(w in l for l in lines), "读串结果和 glm53-test 那次不同（缺「%s」）" % w[:48])
    need(sum(1 for l in lines if re.match(r"mc_comment s\d+ ", l)) == 1, "读串不止一条")
    need(any("mc_func result=all_same" in l for l in lines), "mc_func 不是 all_same")
    need(not any("mc_bytes skipped=memory" in l for l in lines), "内存余量不够，没跑字节比对（%s MB）" % kv_of(lines).get("mem_room_mb"))
    need(not any(l.startswith("mc_bytes") and " only=" in l for l in lines), "字节比对有 only= 行")
    need(any(l.startswith("mc_bytes result=same_after_normalization") for l in lines), "字节比对不是 same_after_normalization")
    return "例外核对和 glm53-test 那次相同"


def j_prep(S, keep, old):
    b = S.get("backup", [])
    k = kv_of(b)
    need(k.get("act") == "ok", "备份失败：%s" % (next((l for l in b if l.startswith("act=fail")), "?")[9:80]))
    need(k.get("bk_vllm") == "23" and k.get("bk_ascend") == "10", "备份个数不是 23、10（%s、%s）" % (k.get("bk_vllm"), k.get("bk_ascend")))
    need("copied_out=1" in b, "备份拷不出容器")
    hs = b[b.index("host_sums_done") - 1] if "host_sums_done" in b else "x"
    need("host_sums_done" in b and not any(": FAILED" in l for l in b), "宿主机上的备份 sha256 不对")
    keep["bk_sums"] = k.get("bk_sums", "")
    r = S.get("replay", [])
    need("replay_exit=0" in r and any("changes=29/29 py_compile=ok" in l for l in r), "p2d 重放没有成功")
    s = [l.split()[1] for l in r if l.startswith("s58 ")]
    need(s, "p2d 结果没有写出来")
    keep["s58"] = s[0]
    return "备份 23+10 个，p2d 结果 %s" % s[0][:16]


def j_prep2(S, keep, old):
    n = S.get("nodefiles", [])
    files = "pd_diag_logfilter.py pd_diag_logging.json segments.json".split()
    need(len(n) >= 2 and all(f in n[0] for f in files) and all(f in n[1] for f in files), "宿主机或容器里看不到三个节点文件")
    h = [l.split()[1] for l in n if l.startswith("host ") and len(l.split()) > 1]
    c = [l.split("devino=")[-1] for l in n if l.startswith("cont ")]
    need(h and c and h[0] == c[0], "容器里的 /data/pd-diag/conf 不是宿主机的目录")
    w = kv_of(S.get("writable", []))
    need(w.get("writable") == "ok", "节点日志目录对 vLLM 的运行用户：%s" % {"bad": "不可写", "unsure": "算不出来（带 ACL）"}.get(w.get("writable"), w.get("writable")))
    return "节点文件在，日志目录可写"


def j_stopped(S, keep, old):
    s = S.get("stopped", [])
    k = kv_of(s)
    need(k.get("running") == "true", "容器没在运行（%s）：整停时不要停容器" % k.get("running"))
    need(k.get("cid") == old.get("cid"), "容器被重建过（ID 变了）：补丁和备份都不在了，停下")
    need(k.get("vllm_procs") == "0", "还有 %s 个 vLLM 进程没停" % k.get("vllm_procs"))
    lost = [l[5:] for l in s if l.startswith("lost ")]
    if lost:   # the container's /tmp was emptied: ④ redoes the kit and the p2d result; rollback uses the monitor's copy
        return "已停，容器没变（容器里 ② 准备的文件没了：④ 重做，回滚用监控机上的备份）"
    s58 = [l.split()[1] for l in s if l.startswith("s58 ")]
    need(s58 and s58[0] == old.get("s58"), "p2d 结果和 ② 不同")
    need(k.get("verify") == "ok", "容器里的备份 sha256 不对")
    return "已停，容器没变"


def j_swap(S, keep, old):
    s = S.get("swap", [])
    need("vllm_procs=0" in s, "换文件时容器里还有 vLLM 进程")
    need("kit_in=fail" not in s, "容器里 ② 准备的文件没了，重新拷包也没成：容器的 /tmp 能写吗")
    fails = [l for l in s if l.startswith("act=fail")]
    need(not fails, "换文件失败：%s" % (fails[0][9:90] if fails else ""))
    need(sum(1 for l in s if l == "act=ok") >= (3 if E.get("MOONCAKE") == "replace" else 2), "换文件没有做完")
    need("exports=ok" in s, "启动文件里的两行 export 不对")
    if E.get("MOONCAKE") == "replace":
        need("mc_procs=0" in s, "还有进程映射着 Mooncake 库")
    if E.get("FIRST") == "1":
        im = [l for l in S.get("import", []) if l.strip() and not l.startswith("ssh_exit=")]
        if E.get("LAB_NO_IMPORT") == "1":   # lab only: the lab image has no torch, the connector cannot be imported
            return "换好（实验室：没有 torch，跳过导入核对）"
        need(any(l.startswith("import_conn True") for l in im), "导入核对失败：%s" % ((im or ["?"])[-1].strip()[:80]))
        if E.get("MOONCAKE") == "replace":
            need(any(l.startswith("import_mc ") for l in im), "Mooncake 导入核对失败：%s" % ((im or ["?"])[-1].strip()[:80]))
        keep["import"] = next(l for l in im if l.startswith("import_conn"))[12:]
        return "换好，导入核对通过（%s）" % keep["import"]
    return "换好（容器里 ② 的准备文件没了，重做了）" if "prepared=redo" in s else "换好"


def startup_counts(lines):
    c = {}
    for l in lines:
        m = re.match(r"S startup (\d+) (.*)$", l)
        if m:
            c[m.group(2)] = c.get(m.group(2), 0) + int(m.group(1))
    return c


def j_started(S, keep, old, late=False):
    if not late:
        h = [l.split() for l in S.get("health", []) if l.startswith("health ")]
        bad = [x[1] for x in h if x[2] != "200"]
        need(len(h) == len(ports_list()) and not bad, "/health 不是 200 的端口：%s" % (span(map(int, bad)) if bad else "没有结果"))
        p = kv_of(S.get("procs", []))
        need(p.get("cmdline_sha16") == old.get("cmdline_sha16"), "vLLM 的命令行和打补丁前不同")
        need(p.get("env_VLLM_LOGGING_CONFIG_PATH") == "/data/pd-diag/conf/pd_diag_logging.json",
             "有 vLLM 进程没设 VLLM_LOGGING_CONFIG_PATH：两行 export 没生效")
        need(p.get("env_PD_DIAG_LEVEL") == "<unset>", "启动环境里设了 PD_DIAG_LEVEL")
        judge_detect(det_lines(S.get("detect", [])), "post", keep, old)
        if E.get("MC_SWAPPED") == "1":
            m = kv_of(S.get("mcmaps", []))
            need(m.get("mc_old") == "0" and int(m.get("mc_procs", "0")) > 0, "有进程用的不是新的 Mooncake 库")
    sc = S.get("scan", [])
    k = kv_of(sc)
    need(k.get("tracebacks") == "0", "vLLM 日志里有本套补丁模块的报错（%s 处）" % k.get("tracebacks"))
    need(k.get("pd_diag_raw") == "0", "[PD_DIAG] 进了 vLLM 日志（%s 行）：日志配置没生效" % k.get("pd_diag_raw"))
    c = startup_counts(sc)
    n = len(ports_list())
    tp = int(E.get("EXP_P_TP", "16"))
    lv = sum(v for kk, v in c.items() if re.match(r"diag level=", kk))
    lv3 = sum(v for kk, v in c.items() if re.match(r"diag level=3 source=(default|env)", kk))
    need(lv == lv3, "启动行里有不是 3 级的 diag level 行")
    want = n * (1 + tp) if E["ROLE"] == "P" else 2 * n
    need(lv3 == want, "启动行 diag level= 有 %d 行，应为 %d 行" % (lv3, want))
    sched = {kk.split()[0]: v for kk, v in c.items() if kk.startswith("scheduler=")}
    if E["ROLE"] == "D":
        need(sched.get("scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler", 0) == n,
             "D 的调度器不是 AsyncRecomputeScheduler（%s）" % ",".join("%s×%d" % (kk.split(".")[-1], v) for kk, v in sched.items()))
        miss = sum(v for kk, v in c.items() if kk.startswith("worker device_timing=on tp_rank=0") and "runner_stages=" not in kk)
        need(miss == 0, "有 D 实例被判成了「不是 D」（device_timing=on 却没有 runner_stages=）")
        rs = sum(v for kk, v in c.items() if kk.startswith("worker device_timing=on tp_rank=0 runner_stages=" + E.get("EXP_D_STAGES", "forward,sampling,prepare_inputs,lm_head,draft,bookkeeping") + " "))
        keep["d_stage_lines"] = str(rs)
        if late:
            need(rs == n, "D 带 runner_stages= 的启动行 %d 条，应为 %d 条" % (rs, n))
    else:
        need(sched.get("scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler", 0) == n,
             "P 的调度器不是 BalanceScheduler")
        need(not any("runner_stages=" in kk for kk in c), "P 被判成了 D（启动行带 runner_stages=）")
        need(not any(kk.startswith("scheduler=") and "balance=True" in kk for kk in c), "BalanceScheduler 带 balance=True")
    if late:
        return "启动行齐了"
    return "health 200×%d，三项 ok(t58)，启动行对" % n + ("" if E["ROLE"] == "P" or keep.get("d_stage_lines") == str(n) else "（D 设备行 %s/%d，⑥ 再数）" % (keep["d_stage_lines"], n))


def j_rb_check(S, keep, old):
    k = kv_of(S.get("rbcheck", []))
    need(k.get("cid") == old.get("cid"), "容器被重建过（ID 变了）")
    if k.get("bk_lost") == "1":
        return "容器里的备份没了，还原时用监控机上的那份（② 核过）"
    need(k.get("verify") == "ok", "容器里的备份 sha256 不对")
    return "备份在"


def j_rb_restore(S, keep, old):
    s = S.get("restore", [])
    need("vllm_procs=0" in s, "还原时容器里还有 vLLM 进程")
    need("cid=" + old.get("cid", "?") in s, "容器被重建过（ID 变了）")
    fails = [l for l in s if l.startswith("act=fail")]
    need(not fails, "还原失败：%s" % (fails[0][9:90] if fails else ""))
    k = kv_of(s)
    need(k.get("connector_sha16") == E["EXP_CONN"], "mooncake_connector.py 没有回到现场原样")
    need(k.get("exports") in ("absent",), "启动文件里还有 export")
    return "还原好"


def j_rb_started(S, keep, old):
    h = [l.split() for l in S.get("health", []) if l.startswith("health ")]
    bad = [x[1] for x in h if x[2] != "200"]
    need(len(h) == len(ports_list()) and not bad, "/health 不是 200 的端口：%s" % (span(map(int, bad)) if bad else "没有结果"))
    p = kv_of(S.get("procs", []))
    need(p.get("cmdline_sha16") == old.get("cmdline_sha16"), "vLLM 的命令行和打补丁前不同")
    d = det_lines(S.get("detect", []))
    need(re.search(r"verdict vllm=unpatched ascend=unpatched\(site_local=\d+\) p2d=unpatched", d.get("verdict", "")),
         "回滚后检测不是三项 unpatched")
    need(d.get("baseline_same"), "回滚后运行模式和打补丁前不同")
    return "回到现场原样"


def j_px(stage, S, keep, old):
    s = S.get("proxy", [])
    k = kv_of(s)
    if stage == "pxpre":
        need(k.get("running") == "true", "proxy 的容器没在运行")
        need(k.get("proxy_procs") == "1", "proxy 主进程有 %s 个（只支持 1 个）" % k.get("proxy_procs"))
        need(k.get("proxy_file_sha") == E["PROXY_ORIG_SHA256"], "现场 proxy 文件和改好的那份所对应的原文件不同")
        need(k.get("proxy_env") == "unset", "proxy 已经带 PD_PROXY_DIAG 变量")
        keep["px_diag_count0"] = k.get("proxy_diag_count", "")
        return "proxy 文件是预期的原文件"
    if stage == "pxprep":
        res = [l for l in s if l.startswith("RESULT:")]
        need(res and res[-1].strip() == "RESULT: OK", "proxy 核对：%s" % (res[-1][8:80] if res else "没有结果"))
        bk = [l.split()[1] for l in s if l.startswith("pxbk ")]
        need(bk and bk[0] == E["PROXY_ORIG_SHA256"], "proxy 原文件备份不对")
        nw = [l.split()[1] for l in s if l.startswith("pxnew ")]
        need(nw and nw[0] == E["PROXY_NEW_SHA256"], "拷进容器的新 proxy 文件不对")
        h = [l.split()[1] for l in s if l.startswith("host ")]
        c = [l.split("devino=")[-1] for l in s if l.startswith("cont ")]
        need(h and c and h[0] == c[0], "proxy 容器里的 /data/pd-diag/logs 不是宿主机的目录")
        return "proxy 核对通过，原文件已备份"
    if stage == "pxswap":
        need(k.get("act") == "ok", "换 proxy 文件失败：%s" % next((l for l in s if l.startswith("act=fail")), "?")[9:80])
        sw = kv_of([x for l in s for x in l.split()])
        keep["px_swap_uptime"], keep["px_boot_id"] = sw.get("swap_uptime", "0"), sw.get("boot_id", "-")
        return "proxy 文件换好"
    if stage == "pxstarted":
        need(k.get("proxy_procs") == "1", "proxy 主进程有 %s 个" % k.get("proxy_procs"))
        need(k.get("proxy_file_sha") == E["PROXY_NEW_SHA256"], "proxy 文件不是新的那份")
        bt = kv_of([x for l in s for x in l.split()])
        # seconds since boot, the clock of /proc/<pid>/stat; a reboot in between (other boot_id) restarted it anyway
        started = int(k.get("proxy_start_ticks", "0")) / int(bt.get("hz", "100"))
        need(bt.get("boot_id") != old.get("px_boot_id") or started > float(old.get("px_swap_uptime", "0")),
             "proxy 没有在换文件以后重启过")
        need("PD_PROXY_DIAG=1" in k.get("proxy_env", "") and "PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy" in k.get("proxy_env", ""),
             "proxy 主进程没带两个环境变量 PD_PROXY_DIAG=1、PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy：proxy 的启动脚本里加上，再重启 proxy")
        keep["proxy_lines"] = k.get("proxy_lines", "0")
        return "proxy 带两个变量，已写 %s 行" % k.get("proxy_lines", "0")
    if stage == "pxrestore":
        need(k.get("act") == "ok", "proxy 文件还原失败")
        return "proxy 文件已还原"


def main():
    stage, log, kvf = sys.argv[1:4]
    text = open(log, encoding="utf-8", errors="replace").read()
    S = sections(text)
    old = load_kv(kvf)
    keep = keep_of(text)
    old_and_new = dict(old)
    old_and_new.update(keep)
    try:
        if stage.startswith("px"):
            msg = j_px(stage, S, keep, old_and_new)
        else:
            msg = {"pre": j_pre, "prep": j_prep, "prep2": j_prep2, "stopped": j_stopped, "swap": j_swap,
                   "started": j_started, "late": lambda S, k, o: j_started(S, k, o, late=True),
                   "rb_check": j_rb_check, "rb_restore": j_rb_restore, "rb_started": j_rb_started,
                   "mcexc": lambda S, k, o: j_mcexc(S, k)}[stage](S, keep, old_and_new)
        line = "OK   " + msg
    except Fail as e:
        line = "FAIL " + str(e)
    with open(kvf, "a") as f:
        for k, v in keep.items():
            f.write("%s=%s\n" % (k, v))
    print(line)


if __name__ == "__main__":
    main()
