#!/usr/bin/env python3
"""The cluster-side check of the fetcher and its output queue: calls the status endpoint GET /pd_diag/v1/status of
interface one and prints one line per source (instance, proxy worker), then the output queue, the segment definition
and the fetcher's own counters, then RESULT: OK or RESULT: FAIL. Read-only: only that one GET request.

  python3 fetcher_check.py [--url http://<proxy 机器>:9300]

Run on the proxy host or on any machine that reaches the fetcher's listen_port. With more than 30 sources, the sources
without findings are summarised in one line. The counters are those since the fetcher started. Exit code 1 on FAIL.
Standard library only; Python 3.9 and later.
"""
import argparse
import json
import sys
import urllib.request

INTERFACE = "pd_diag/v1"
LAST_OK_LIMIT = 35       # fetch_backoff_max_seconds (30 by default) plus the fetch timeout and a margin
BEHIND_LIMIT = 150       # incomplete_idle_seconds (120 by default) plus a margin
MANY = 30


def human(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024


def judge_source(s):
    """(level, text) of one source of the status endpoint."""
    if s.get("gone"):
        return "OK", "已退出（套接字不在），不再读取"
    bad, warn = [], []
    last_ok = s.get("last_ok_seconds")
    if s.get("run") is None or last_ok is None:
        bad.append("采集进程启动以来没有读取成功过")
    elif last_ok > LAST_OK_LIMIT:
        bad.append("最近一次读取成功在 %.0f 秒前" % last_ok)
    if s.get("behind_seconds", 0) > BEHIND_LIMIT:
        warn.append("等待这个来源的请求已 %d 秒" % s["behind_seconds"])
    if s.get("ring_overwritten_lines"):
        warn.append("%d 条记录在读出之前被缓冲区覆盖" % s["ring_overwritten_lines"])
    head, read = s.get("head_seq"), s.get("read_seq")
    text = "运行号 %s，已读序号 %s / 最新 %s，最近成功 %s 秒前，读取失败 %d 次、退避 %d 次、运行号变化 %d 次，覆盖 %d 条，behind %d 秒" % (
        s.get("run") or "—", read, "—" if head is None else head, "—" if last_ok is None else "%.0f" % last_ok,
        s.get("errors", 0), s.get("backoffs", 0), s.get("run_changes", 0), s.get("ring_overwritten_lines", 0), s.get("behind_seconds", 0))
    if s.get("last_error") and (bad or s.get("errors")):
        text += "，最近的错误：%s" % str(s["last_error"])[:120]
    if bad or warn:
        text = "；".join(bad + warn) + "（" + text + "）"
    if s.get("kind") == "proxy":
        return "INFO", text + "；proxy 可选：它的状态只报告，不影响结论，没有 proxy 记录的请求按 no_proxy 写出"
    return ("FAIL" if bad else "WARN" if warn else "OK"), text


def main(argv=None):
    ap = argparse.ArgumentParser(description="采集进程和写出队列的检查（集群端，只读）")
    ap.add_argument("--url", default="http://127.0.0.1:9300", help="采集进程的地址 http://<proxy 机器>:<listen_port>，默认 http://127.0.0.1:9300")
    ap.add_argument("--timeout", type=float, default=10)
    a = ap.parse_args(argv)
    url = a.url.rstrip("/") + "/pd_diag/v1/status"
    try:
        with urllib.request.urlopen(url, timeout=a.timeout) as r:
            st = json.load(r)
    except Exception as e:  # noqa: BLE001
        print("FAIL 采集进程没有应答（%s：%s）→ 确认采集进程在运行：docker exec <proxy 容器> pgrep -f pd_fetcher.py；"
              "没有运行时按部署说明手动启动" % (url, str(e)[:160]))
        print("RESULT: FAIL")
        return 1
    fails = 0

    def line(level, text):
        nonlocal fails
        fails += level == "FAIL"
        print("%-4s %s" % (level, text))

    f = st.get("fetcher") or {}
    line("OK" if st.get("interface") == INTERFACE else "FAIL", "采集进程：集群 %s，接口 %s，启动于 %s" % (
        st.get("cluster"), st.get("interface"), f.get("started")) + ("" if st.get("interface") == INTERFACE else "（本工具按 %s 检查）" % INTERFACE))
    sources = st.get("sources") or []
    judged = [(s, judge_source(s)) for s in sources]
    quiet = len(judged) > MANY
    good = []
    for s, (lv, text) in judged:
        if quiet and lv == "OK":
            good.append(s["name"])
            continue
        kind = "proxy worker" if s.get("kind") == "proxy" else "实例"
        line(lv, "%s %s：%s" % (kind, s["name"], text))
    if good:
        line("OK", "其余 %d 个来源正常：%s" % (len(good), " ".join(good[:20]) + (" 等" if len(good) > 20 else "")))
    if not any(s.get("kind") == "proxy" and not s.get("gone") for s in sources):
        line("INFO", "proxy worker：没有找到套接字（proxy 未打补丁、未运行或仍是旧补丁）；请求按 no_proxy 写出，proxy 段为空，端到端时延从实例起算")
    if not any(s.get("kind") == "instance" for s in sources):
        line("FAIL", "没有任何实例来源：fetcher.json 的 instances 为空")
    line("OK", "写出队列：最新序号 %s，最旧序号 %s，占用 %s；请求摘要日志占用 %s；已写出请求文档 %s 份" % (
        st.get("head_seq"), st.get("oldest_seq"), human(f.get("spool_bytes")), human(f.get("summary_bytes")), f.get("docs_written")))
    mism = f.get("config_mismatch") or []
    line("FAIL" if mism else "OK", "段定义版本 %s%s" % (st.get("config_version"), "；版本不同的实例（config_mismatch）：%s → 下发与采集进程同一版的 segments.json" % " ".join(mism) if mism else "，各实例相同"))
    recent = (f.get("segment_changes_recent") or [])[-1:]
    line("WARN" if f.get("segments_errors") else "OK", "段定义变化 %s 次%s%s" % (
        f.get("segment_changes", 0), "，最近一次 %s：%s → %s" % (recent[0].get("ts"), recent[0].get("from"), recent[0].get("to")) if recent else "",
        "；segments.json 读取失败 %d 次，沿用上一版" % f["segments_errors"] if f.get("segments_errors") else ""))
    v = f.get("log_dropped", 0)
    line("WARN" if v else "OK", "写出队列在读取之前删除的文档（log_dropped）%d 份%s" % (v, "：监控端停机时间超过了写出队列可容纳的时长" if v else ""))
    v = f.get("log_write_errors", 0)
    line("FAIL" if v else "OK", "写出队列、请求摘要日志写入失败（log_write_errors）%d 次%s" % (v, " → 检查 proxy 机器的磁盘空间" if v else ""))
    v = f.get("late_dropped", 0)
    line("WARN" if v else "OK", "超过延迟到达窗口被丢弃的记录（late_dropped）%d 条" % v)
    closed = f.get("closed") or {}
    line("OK", "在途请求 %s 个；已写出的请求按完成方式：complete %s，no_proxy %s，incomplete %s，evicted %s" % (
        st.get("open_requests"), closed.get("complete"), closed.get("no_proxy"), closed.get("incomplete"), closed.get("evicted")))
    print("RESULT: %s" % ("FAIL" if fails else "OK"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
