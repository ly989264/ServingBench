#!/usr/bin/env python3
"""Compares what this pack would deploy with what the live services use, before a deploy step acts (design 2.13).

Usage: compare.py all|board|ddl|prometheus|central|datasource [--sql-out FILE]   (bin/compare.sh loads deploy.env first)

Works on the pack folder it sits in (its deploy.env, segments.json, inventory.json) and on the live services that
deploy.env names. Toward them it only reads: Grafana GET, ClickHouse queries as a --readonly=1 client inside its
container, docker inspect and docker exec cat, Prometheus GET. It writes only under generated/compare/.

The five items, what is compared, and what is left out:
  看板        the dashboard as grafana_api.py would import it, and its folder  vs  GET /api/dashboards/uid/<uid>;
              id, version and iteration left out (as board_hash of the update script)
  表和视图    generate.py's schema.sql and views.sql  vs  system.columns (names and types), the TTL clause of
              system.tables.create_table_query, the view definitions (whitespace left out; ClickHouse's own
              formatQuerySingleLine when the text still differs, since it stores views reformatted)
  Prometheus  the two blocks prometheus_merge.py add writes (marked with PROMETHEUS_MARK) and the rules file  vs  the same
              blocks in PROMETHEUS_YML and /prometheus/<RULES_FILE> in the container; nothing outside the markers is
              read. Also whether Prometheus has loaded them (its two jobs and the rule group in its API)
  采集器      render_vector.py's central.yaml and inventory.csv, the image and the run arguments of bin/central_up.sh
              vs  the files in the folder the container mounts at /etc/vector, docker inspect; a file changed after the
              container started counts as a difference (the running collector read an earlier one)
  数据源      the data source grafana_api.py would write  vs  GET /api/datasources/uid/<uid>; id, version, readOnly
              and the secrets left out. The password counts as the same when Grafana's health check of the data
              source passes and, where the ClickHouse container is local, vllm_reader logs in with CH_READER_PASSWORD
Pack version strings (v2.0, v2.1, the one in VERSION) are replaced by one placeholder on both sides first.

Output, one line per item (other scripts parse it):
  OK   <项>：不变                  (an OK line may go on with "；<note>", for example a stopped collector)
  INFO <项>：有变化：<哪一处>
  WARN <项>：比不了（<原因>）
`all` prints the five lines, then: RESULT: 不变 n 项，有变化 m 项，比不了 k 项
Exit status: 0 every item unchanged; 10 something changed or could not be compared; 2 usage or environment error.
`ddl --sql-out FILE` (FILE under generated/compare/) writes the statements of schema.sql and views.sql that would change
something, each ending with ";"; an empty file when none would. When ClickHouse cannot be compared, every statement.
Python 3.9 and later, standard library only.
"""
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
SCRIPTS = pathlib.Path(__file__).resolve().parent
PACK = SCRIPTS.parent
OUT = PACK / "generated" / "compare"
sys.path.insert(0, str(SCRIPTS))
ENV = os.environ
ITEMS = [("board", "看板"), ("ddl", "表和视图"), ("prometheus", "Prometheus"), ("central", "采集器"), ("datasource", "数据源")]
LABEL = dict(ITEMS)
PLACEHOLDER = "@@PACK_VERSION@@"
# v2.0, v2.1 and the like, not part of a longer name or number (v2.0.1, pd-diag-v2-..., vllm_pd.request.v2).
VERSION_RE = re.compile(r"(?<![\w.])v\d+\.\d+(?![\w]|\.\d)")
# The label bin/central_up.sh puts on the collector container: sha of CENTRAL_DOCKER_ARGS, which docker inspect cannot show.
ARGS_LABEL = "vllm-pd.docker-args"


class Unavailable(Exception):
    """The live side cannot be read: the item is WARN 比不了."""


# ==================== pure helpers (tests call these without any service) ====================
def pack_version():
    try:
        return (PACK / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def unversion(text, version=None):
    """text with the pack's version strings replaced by one placeholder."""
    v = pack_version() if version is None else version
    if v:
        text = text.replace(v, PLACEHOLDER)
    return VERSION_RE.sub(PLACEHOLDER, text)


def unversion_json(value, version=None):
    return json.loads(unversion(json.dumps(value, ensure_ascii=False, sort_keys=True), version))


def ws(text):
    return re.sub(r"\s+", " ", text or "").strip()


def args_sha(args):
    return hashlib.sha256((args or "").encode("utf-8")).hexdigest()[:16]


MISSING = object()


def first_diff_path(a, b, path=""):
    """The first key path where two JSON values differ (targets[0].rawSql), None when they are equal."""
    if a == b:
        return None
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            x, y = a.get(k, MISSING), b.get(k, MISSING)
            if x is MISSING or y is MISSING or x != y:
                sub = "%s.%s" % (path, k) if path else str(k)
                return sub if (x is MISSING or y is MISSING) else first_diff_path(x, y, sub)
    if isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else MISSING
            y = b[i] if i < len(b) else MISSING
            if x is MISSING or y is MISSING or x != y:
                sub = "%s[%d]" % (path, i)
                return sub if (x is MISSING or y is MISSING) else first_diff_path(x, y, sub)
    return path or "整个值"


def normalize_board(board, version=None):
    """The dashboard without the fields every import changes, with the version placeholder."""
    d = {k: v for k, v in board.items() if k not in ("id", "version", "iteration")}
    return unversion_json(d, version)


def panel_change(ours, live):
    """Where two panel lists differ first: the panel's title and the key, rows looked into."""
    for i in range(max(len(ours), len(live))):
        a = ours[i] if i < len(ours) else MISSING
        b = live[i] if i < len(live) else MISSING
        if a == b:
            continue
        p = a if a is not MISSING else b
        title = (p.get("title") if isinstance(p, dict) else None) or "第 %d 个 panel" % (i + 1)
        if a is MISSING:
            return "panel「%s」现场有、包里没有" % title
        if b is MISSING:
            return "panel「%s」现场没有" % title
        if isinstance(a, dict) and isinstance(b, dict) and isinstance(a.get("panels"), list) and isinstance(b.get("panels"), list) \
                and a.get("panels") != b.get("panels") and {k: v for k, v in a.items() if k != "panels"} == {k: v for k, v in b.items() if k != "panels"}:
            return panel_change(a["panels"], b["panels"])
        return "panel「%s」的 %s" % (title, first_diff_path(a, b))
    return None


def board_change(ours, live):
    """None when equal, else where the two normalized dashboards differ first."""
    if ours == live:
        return None
    op, lp = ours.get("panels"), live.get("panels")
    if isinstance(op, list) and isinstance(lp, list) and op != lp:
        return panel_change(op, lp)
    return "看板的 %s" % first_diff_path(ours, live)


# ---- SQL text ----
def mask(sql):
    """sql with every quoted part ('...', "...", `...`) replaced by x of the same length, so that structure can be read
    from it with plain regexes: indexes match the original text."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c != "`":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            j = min(j, n - 1)
            out.append("x" * (j - i + 1))
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def split_sql(text):
    """The statements of a SQL script, comments left out, each without its final ";" and stripped."""
    m = mask(text)
    out, start, i = [], 0, 0
    keep = []
    while i < len(text):
        if m.startswith("--", i):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            keep.append(text[start:i])
            start = i = j
            continue
        if m[i] == ";":
            keep.append(text[start:i])
            s = "".join(keep).strip()
            if s:
                out.append(s)
            keep, start = [], i + 1
        i += 1
    keep.append(text[start:])
    s = "".join(keep).strip()
    if s:
        out.append(s)
    return out


def depths(m):
    d, out = 0, []
    for c in m:
        if c == "(":
            d += 1
            out.append(d)
        elif c == ")":
            out.append(d)
            d -= 1
        else:
            out.append(d)
    return out


def top_find(sql, pattern, start=0):
    """First match of pattern (on the masked text) at parenthesis depth 0, at or after start: a match object or None."""
    m = mask(sql)
    dp = depths(m)
    for x in re.finditer(pattern, m[start:]):
        k = start + x.start()
        if dp[k] == 0 and m[k] != ")":
            return re.compile(pattern).match(m, k)
    return None


def paren_span(sql, open_at):
    """Index of the ")" that closes the "(" at open_at."""
    m = mask(sql)
    d = 0
    for i in range(open_at, len(m)):
        if m[i] == "(":
            d += 1
        elif m[i] == ")":
            d -= 1
            if d == 0:
                return i
    raise ValueError("括号不配对")


def top_split(sql):
    """sql split at commas of parenthesis depth 0."""
    m = mask(sql)
    dp = depths(m)
    parts, start = [], 0
    for i, c in enumerate(m):
        if c == "," and dp[i] == 0:
            parts.append(sql[start:i])
            start = i + 1
    parts.append(sql[start:])
    return [p.strip() for p in parts if p.strip()]


COLUMN_END = r"\b(?:DEFAULT|MATERIALIZED|ALIAS|EPHEMERAL|CODEC|TTL|COMMENT|NOT NULL|NULL|STATISTICS|SETTINGS|PRIMARY KEY)\b"


def unquote_name(name):
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "`\"":
        return name[1:-1]
    return name


def parse_columns(body):
    """{column: type} of a CREATE TABLE column list (the text inside its parentheses); indexes and constraints left out."""
    cols = {}
    for el in top_split(body):
        first = re.match(r"\s*(\S+)\s+", mask(el))
        if not first:
            continue
        word = el[first.start(1):first.end(1)]
        if word.upper() in ("INDEX", "PROJECTION", "CONSTRAINT", "PRIMARY"):
            continue
        rest = el[first.end():]
        end = top_find(rest, COLUMN_END)
        cols[unquote_name(word)] = ws(rest[:end.start()] if end else rest)
    return cols


def parse_create_table(stmt):
    """(table, {column: type}, TTL clause or None) of a CREATE TABLE statement, ours or ClickHouse's own."""
    m = re.match(r"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)\s*\(", mask(stmt), re.I)
    if not m:
        raise ValueError("不是 CREATE TABLE")
    table = stmt[m.start(1):m.end(1)].replace("`", "")
    open_at = m.end() - 1
    close = paren_span(stmt, open_at)
    return table, parse_columns(stmt[open_at + 1:close]), ttl_clause(stmt[close + 1:])


def ttl_clause(after_columns):
    """The table TTL in the part of a CREATE TABLE after its column list, None when there is none."""
    t = top_find(after_columns, r"\bTTL\b")
    if not t:
        return None
    end = top_find(after_columns, r"\b(?:SETTINGS|COMMENT)\b", t.end())
    return ws(after_columns[t.end():end.start() if end else len(after_columns)])


INTERVAL_UNITS = {"SECOND": "Second", "MINUTE": "Minute", "HOUR": "Hour", "DAY": "Day", "WEEK": "Week", "MONTH": "Month",
                  "QUARTER": "Quarter", "YEAR": "Year"}


def norm_ttl(ttl):
    """A TTL expression as ClickHouse prints it back: INTERVAL 15 DAY is toIntervalDay(15), whitespace collapsed."""
    t = ws(ttl)
    t = re.sub(r"\bINTERVAL\s+'?(\d+)'?\s+(SECOND|MINUTE|HOUR|DAY|WEEK|MONTH|QUARTER|YEAR)S?\b",
               lambda m: "toInterval%s(%s)" % (INTERVAL_UNITS[m.group(2).upper()], m.group(1)), t, flags=re.I)
    t = re.sub(r"\(\s+", "(", t)
    return re.sub(r"\s+\)", ")", t)


def ttl_text(ttl):
    """Short form for the output line: 15 天, else the expression."""
    m = re.fullmatch(r"toDate\(ts\) \+ toIntervalDay\((\d+)\)", ttl or "")
    return "%s 天" % m.group(1) if m else (ttl or "没有")


def view_select(stmt):
    """(view, SELECT text) of CREATE [OR REPLACE] VIEW, ours or ClickHouse's own (which lists the columns first)."""
    m = re.match(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)", mask(stmt), re.I)
    if not m:
        raise ValueError("不是 CREATE VIEW")
    name = stmt[m.start(1):m.end(1)].replace("`", "")
    name = re.sub(r"\($", "", name)
    a = top_find(stmt, r"\bAS\b", m.end())
    if not a:
        raise ValueError("视图里没有 AS")
    return name, stmt[a.end():].strip()


def short(name):
    return name.split(".", 1)[1] if name.startswith("vllm_pd.") else name


class LiveDB:
    """What ClickHouse has: whether the database exists, and per table (vllm_pd.<name>) its engine, columns, TTL and,
    for a view, its SELECT."""

    def __init__(self, database_exists, tables):
        self.database_exists = database_exists
        self.tables = tables

    @classmethod
    def from_rows(cls, database_exists, table_rows, column_rows):
        """table_rows: dicts name, engine, create_table_query (system.tables); column_rows: dicts table, name, type
        (system.columns), both for database vllm_pd."""
        tables = {}
        for r in table_rows:
            key = "vllm_pd." + r["name"]
            t = {"engine": r.get("engine", ""), "cols": {}, "ttl": None, "select": None}
            q = r.get("create_table_query") or ""
            if t["engine"] == "View":
                try:
                    t["select"] = view_select(q)[1]
                except ValueError:
                    t["select"] = None
            elif q:
                try:
                    t["ttl"] = parse_create_table(q)[2]
                except ValueError:
                    pass
            tables[key] = t
        for r in column_rows:
            key = "vllm_pd." + r["table"]
            if key in tables:
                tables[key]["cols"][r["name"]] = r["type"]
        return cls(database_exists, tables)


def ddl_plan(stmts, live, same_sql=None):
    """Which statements of schema.sql + views.sql would change something on `live` (a LiveDB).
    same_sql(a, b) -> bool tells whether two SELECTs are the same query once ClickHouse formats them; it raises
    Unavailable when it cannot tell. Returns (statements to run, changes, problems): changes are short texts for the
    output line, problems the reasons something could not be compared (their statements are run)."""
    tables = {k: {"cols": dict(v["cols"]), "ttl": v["ttl"], "select": v["select"], "engine": v["engine"]} for k, v in live.tables.items()}
    db = live.database_exists
    run, changes, problems = [], [], []
    added, expected = {}, {}
    for s in stmts:
        w = ws(s)
        m = re.match(r"CREATE DATABASE IF NOT EXISTS (\S+)$", w, re.I)
        if m:
            if not db:
                run.append(s)
                changes.append("建库 %s" % m.group(1))
                db = True
            continue
        m = re.match(r"CREATE TABLE IF NOT EXISTS (\S+) ", w, re.I)
        if m:
            name, cols, ttl = parse_create_table(s)
            expected[name] = cols
            if name not in tables:
                run.append(s)
                changes.append("建表 %s" % short(name))
                tables[name] = {"cols": dict(cols), "ttl": ttl, "select": None, "engine": "MergeTree"}
            else:
                for c, t in cols.items():
                    have = tables[name]["cols"].get(c)
                    if have is not None and ws(have) != ws(t):
                        changes.append("列 %s.%s 的类型：现场 %s，包里 %s（DDL 不改列类型，要手工处理）" % (short(name), c, have, t))
            continue
        m = re.match(r"ALTER TABLE (\S+) ADD COLUMN IF NOT EXISTS (\S+) (.+)$", w, re.I)
        if m:
            name, col = m.group(1), unquote_name(m.group(2))
            t = parse_columns("%s %s" % (m.group(2), m.group(3))).get(col, m.group(3))
            have = tables.get(name, {}).get("cols", {}).get(col)
            if name not in tables or have is None:
                run.append(s)
                added.setdefault(short(name), []).append(col)
                tables.setdefault(name, {"cols": {}, "ttl": None, "select": None, "engine": ""})["cols"][col] = t
            elif ws(have) != ws(t):
                changes.append("列 %s.%s 的类型：现场 %s，包里 %s（DDL 不改列类型，要手工处理）" % (short(name), col, have, t))
            continue
        m = re.match(r"ALTER TABLE (\S+) MODIFY TTL (.+)$", w, re.I)
        if m:
            name, want = m.group(1), norm_ttl(m.group(2))
            have = tables.get(name, {}).get("ttl")
            if have is not None and unversion(norm_ttl(have)) == unversion(want):
                continue
            if have is not None and same_sql is not None:
                try:
                    if same_sql("SELECT " + have, "SELECT " + m.group(2)):
                        continue
                except Unavailable as e:
                    problems.append("TTL %s：%s" % (short(name), e))
            run.append(s)
            changes.append("TTL %s：%s → %s" % (short(name), ttl_text(norm_ttl(have)) if have else "没有", ttl_text(want)))
            if name in tables:
                tables[name]["ttl"] = want
            continue
        m = re.match(r"CREATE OR REPLACE VIEW (\S+) AS ", w, re.I)
        if m:
            name, sel = view_select(s)
            have = tables.get(name, {}).get("select")
            if have is not None and ws(unversion(have)) == ws(unversion(sel)):
                continue
            if have is not None and same_sql is not None:
                try:
                    if same_sql(have, sel):
                        continue
                except Unavailable as e:
                    problems.append("视图 %s：%s" % (short(name), e))
                    run.append(s)
                    continue
            run.append(s)
            changes.append(("建视图 %s" if have is None else "视图 %s 的定义") % short(name))
            continue
        run.append(s)
        changes.append("其他语句：%s" % w[:60])
    for t, cols in added.items():
        changes.append("加列 %s.%s" % (t, "、".join(cols)))
    # Columns of a table's definition that no ADD COLUMN brings: the DDL cannot add them to a table that exists.
    for name, cols in expected.items():
        miss = [c for c in cols if c not in tables.get(name, {}).get("cols", {})]
        if miss:
            changes.append("列 %s.%s 现场没有，DDL 不会加（要手工处理）" % (short(name), "、".join(miss)))
    return run, changes, problems


def summarize(changes, limit=3):
    if len(changes) <= limit:
        return "；".join(changes)
    return "；".join(changes[:limit]) + "；另有 %d 处" % (len(changes) - limit)


def text_change(name, ours, live, masks=("password",)):
    """None when equal, else 'name 第 N 行（包里：…）' with secrets masked."""
    if ours == live:
        return None
    a, b = ours.splitlines(), live.splitlines()
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            line = x if x is not None else ""
            for k in masks:
                if k in line.lower():
                    line = re.sub(r"(%s\S*\s*[:=]\s*).*" % k, r"\1***", line, flags=re.I)
            line = line.strip()
            if x is None:
                return "%s 第 %d 行起现场多出来" % (name, i + 1)
            return "%s 第 %d 行（包里：%s）" % (name, i + 1, line[:60] + ("…" if len(line) > 60 else ""))
    return "%s 的行尾或末尾空行" % name


def split_jobs(block):
    """{job name: its lines} of a scrape block."""
    out, cur = {}, None
    for line in (block or "").splitlines():
        m = re.match(r'^\s*-\s*job_name:\s*"?([^"\s]+)"?', line)
        if m:
            cur = m.group(1)
            out[cur] = []
        if cur:
            out[cur].append(line)
    return out


def job_change(ours, live):
    a, b = split_jobs(ours), split_jobs(live)
    for j in list(a) + [j for j in b if j not in a]:
        if a.get(j) != b.get(j):
            if j not in b:
                return "job %s（现场没有）" % j
            if j not in a:
                return "job %s（现场多出来）" % j
            return "job %s" % j
    return "抓取任务段的标记行"


def split_rules(text):
    """(text before the first rule, {alert name: its lines})."""
    head, out, cur = [], {}, None
    for line in (text or "").splitlines():
        m = re.match(r"^\s*-\s*alert:\s*(\S+)", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        if cur:
            out[cur].append(line)
        else:
            head.append(line)
    return head, out


def rules_change(ours, live):
    if ours == live:
        return None
    ha, a = split_rules(ours)
    hb, b = split_rules(live)
    if ha != hb:
        return "规则文件开头（规则组名）"
    for r in list(a) + [r for r in b if r not in a]:
        if a.get(r) != b.get(r):
            return "规则 %s%s" % (r, "（现场没有）" if r not in b else "（现场多出来）" if r not in a else "")
    return "规则文件的行尾或末尾空行"


def alert_names(text):
    return sorted(split_rules(text)[1])


def docker_started(text):
    """docker inspect's State.StartedAt (RFC 3339 with nanoseconds, UTC) as seconds since the epoch, None if unreadable."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", text or "")
    if not m or text.startswith("0001-"):
        return None
    t = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    off = 0
    if m.group(3) != "Z":
        sign = 1 if m.group(3)[0] == "+" else -1
        off = sign * (int(m.group(3)[1:3]) * 3600 + int(m.group(3)[4:6]) * 60)
    frac = float("0." + m.group(2)) if m.group(2) else 0.0
    return (t - datetime.datetime(1970, 1, 1)).total_seconds() - off + frac


# ==================== live services (read only) ====================
def run(cmd, input=None, env=None, timeout=60):
    try:
        return subprocess.run(cmd, input=input, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout)
    except FileNotFoundError:
        raise Unavailable("没有 %s 命令" % cmd[0])
    except subprocess.TimeoutExpired:
        raise Unavailable("%s 超过 %d 秒没有返回" % (" ".join(cmd[:3]), timeout))


def secrets():
    return [v for v in (ENV.get(k, "") for k in ("CH_ADMIN_PASSWORD", "CH_WRITER_PASSWORD", "CH_READER_PASSWORD", "GRAFANA_PASSWORD")) if v]


def hide(text):
    for s in secrets():
        text = text.replace(s, "***")
    return text


def last_line(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return hide(lines[-1][:200]) if lines else "?"


_inspect = {}


def docker_inspect(name):
    """docker inspect of a container as a dict, None when there is no such container."""
    if name not in _inspect:
        r = run(["docker", "inspect", "--type", "container", name])
        if r.returncode != 0:
            if "No such" in r.stderr or "no such" in r.stderr:
                _inspect[name] = None
            else:
                raise Unavailable("docker inspect %s 失败：%s" % (name, last_line(r.stderr)))
        else:
            _inspect[name] = json.loads(r.stdout)[0]
    return _inspect[name]


def running(info):
    return bool(info and (info.get("State") or {}).get("Running"))


class ClickHouse:
    """Queries inside the ClickHouse container as a read-only client. The password reaches clickhouse-client through
    docker exec's environment, not its command line."""

    def __init__(self, container):
        self.container = container
        self.failed = None

    def ready(self):
        if self.failed:
            raise Unavailable(self.failed)
        info = docker_inspect(self.container)
        if not running(info):
            self.failed = "ClickHouse 容器 %s %s" % (self.container, "没有" if info is None else "没在运行")
            raise Unavailable(self.failed)

    def query(self, sql, user="default", password=None):
        self.ready()
        pw = ENV.get("CH_ADMIN_PASSWORD", "") if password is None else password
        r = run(["docker", "exec", "-i", "-e", "CLICKHOUSE_PASSWORD", self.container, "clickhouse-client", "--user", user,
                 "--readonly=1", "--format", "JSONEachRow"], input=sql, env=dict(ENV, CLICKHOUSE_PASSWORD=pw), timeout=120)
        if r.returncode != 0:
            raise Unavailable("ClickHouse 查询失败：%s" % last_line(r.stderr))
        return [json.loads(l) for l in r.stdout.splitlines() if l.strip()]

    def live(self):
        rows = self.query("SELECT name FROM system.databases WHERE name = 'vllm_pd'")
        tables = self.query("SELECT name, engine, create_table_query FROM system.tables WHERE database = 'vllm_pd' ORDER BY name")
        cols = self.query("SELECT table, name, type FROM system.columns WHERE database = 'vllm_pd' ORDER BY table, position")
        return LiveDB.from_rows(bool(rows), tables, cols)

    def same_sql(self, a, b):
        """Whether two queries are the same once ClickHouse formats them (formatQuerySingleLine)."""
        lit = lambda s: "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
        try:
            rows = self.query("SELECT formatQuerySingleLine(%s) AS a, formatQuerySingleLine(%s) AS b" % (lit(a), lit(b)))
        except Unavailable as e:
            raise Unavailable("formatQuerySingleLine 用不了（%s）" % e)
        if len(rows) != 1:
            raise Unavailable("formatQuerySingleLine 没有返回结果")
        return unversion(rows[0]["a"]) == unversion(rows[0]["b"])


class Grafana:
    """GET only. One login check per run: after a failed login nothing more is sent (repeated failures can lock the account)."""

    def __init__(self, G):
        self.G = G
        self.failed = None
        self.checked = False

    def _get(self, path):
        try:
            return self.G.call("GET", path)
        except SystemExit:
            self.failed = "Grafana %s 连不上" % self.G.URL
        except (OSError, ValueError) as e:
            self.failed = "Grafana %s 读不了：%s" % (path, e)
        raise Unavailable(self.failed)

    def login(self):
        if not self.checked:
            self.checked = True
            code, body = self._get("/api/org")
            if code in (401, 403):
                self.failed = "Grafana 登录失败（HTTP %d），检查 GRAFANA_USER 和 GRAFANA_PASSWORD" % code
            elif code != 200:
                self.failed = "Grafana /api/org 返回 HTTP %d" % code
        if self.failed:
            raise Unavailable(self.failed)

    def get(self, path):
        self.login()
        return self._get(path)


def http_json(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Unavailable("%s 读不了：%s" % (url.split("?")[0], getattr(e, "reason", e)))


# ==================== the five items ====================
class Ctx:
    """What several items share in one run."""

    def __init__(self):
        self._tree = None
        self._G = None
        self._grafana = None
        self.ch = ClickHouse(ENV.get("CLICKHOUSE_CONTAINER", ""))
        self.sql_out = None

    def tree(self):
        if self._tree is None:
            from segtree import Tree
            self._tree = Tree()
        return self._tree

    def grafana(self):
        if self._grafana is None:
            import grafana_api
            self._G = grafana_api
            self._grafana = Grafana(grafana_api)
        return self._G, self._grafana


def write(name, text, mode=0o644):
    """A file under generated/compare/, created with `mode` (central.yaml holds the writer password: 600)."""
    p = OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(str(p), mode)
    return p


def item_board(ctx):
    import generate
    G, g = ctx.grafana()
    raw = json.loads(json.dumps(generate.build_board(ctx.tree()), ensure_ascii=False, indent=1))
    # prepared_board looks up the two panel 1 dashboards (GET) after the same one login check.
    g.login()
    try:
        board = G.prepared_board(raw, quiet=True)
    except SystemExit:
        raise Unavailable("Grafana %s 连不上" % G.URL)
    ours = normalize_board(board)
    write("board.json", json.dumps(ours, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    code, body = g.get("/api/dashboards/uid/" + G.BOARD_UID)
    if code == 404:
        return "INFO", "Grafana 里没有 UID 为 %s 的看板，会新建" % G.BOARD_UID
    if code != 200 or not isinstance(body.get("dashboard"), dict):
        raise Unavailable("GET /api/dashboards/uid/%s 返回 HTTP %d" % (G.BOARD_UID, code))
    live = normalize_board(body["dashboard"])
    write("board.live.json", json.dumps(live, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    changes = []
    folder = (body.get("meta") or {}).get("folderTitle")
    if folder != G.FOLDER:
        changes.append("所在文件夹「%s」，包里是「%s」" % (folder or "General", G.FOLDER))
    c = board_change(ours, live)
    if c:
        changes.insert(0, c)
    return ("INFO", summarize(changes)) if changes else ("OK", None)


def item_ddl(ctx):
    import generate
    tree = ctx.tree()
    schema, views = generate.schema_sql(tree), generate.views_sql(tree)
    write("schema.sql", schema)
    write("views.sql", views)
    stmts = split_sql(schema) + split_sql(views)
    if ctx.sql_out:
        # Until the plan is known the file holds every statement: whatever goes wrong below, the step runs what v2.0 runs.
        ctx.sql_out.write_text("".join(s + ";\n" for s in stmts), encoding="utf-8")
    live = ctx.ch.live()
    todo, changes, problems = ddl_plan(stmts, live, ctx.ch.same_sql)
    if ctx.sql_out:
        ctx.sql_out.write_text("".join(s + ";\n" for s in todo), encoding="utf-8")
    if problems:
        raise Unavailable(summarize(problems) + ("；另有变化：" + summarize(changes) if changes else ""))
    return ("INFO", summarize(changes)) if changes else ("OK", None)


def item_prometheus(ctx):
    import prometheus_merge as PM
    puller = "%s:%s" % (ENV.get("MONITOR_HOST", ""), ENV.get("PULLER_METRICS_PORT", ""))
    central = ENV.get("CENTRAL_CONTAINER", "")
    rules_ours = unversion(PM.rules_text())
    rb_ours, jb_ours = (unversion(x) for x in PM.blocks(puller, central))
    write(ENV.get("RULES_FILE", "rules.yml"), rules_ours)
    write("prometheus-blocks.yml", rb_ours + jb_ours)
    path = ENV.get("PROMETHEUS_YML", "")
    try:
        with open(path, encoding="utf-8") as f:
            rbs, jbs = PM.marked(f.read())
    except OSError as e:
        raise Unavailable("读不了 PROMETHEUS_YML=%s：%s" % (path, e.strerror or e))
    rbs, jbs = [unversion(x) for x in rbs], [unversion(x) for x in jbs]
    changes = []
    if not jbs:
        changes.append("prometheus.yml 里没有面板二的抓取任务段（%s、%s）" % (ENV.get("PULLER_JOB"), ENV.get("CENTRAL_JOB")))
    elif len(jbs) > 1:
        changes.append("prometheus.yml 里面板二的抓取任务段有 %d 个" % len(jbs))
    elif jbs[0] != jb_ours:
        changes.append(job_change(jb_ours, jbs[0]))
    if not rbs:
        changes.append("prometheus.yml 里没有面板二的 rule_files 段")
    elif len(rbs) > 1 or rbs[0] != rb_ours:
        changes.append("rule_files 段")
    prom = ENV.get("PROMETHEUS_CONTAINER", "")
    info = docker_inspect(prom)
    if not running(info):
        raise Unavailable("Prometheus 容器 %s %s" % (prom, "没有" if info is None else "没在运行"))
    rules_path = "/prometheus/" + ENV.get("RULES_FILE", "")
    r = run(["docker", "exec", prom, "cat", rules_path])
    if r.returncode != 0:
        changes.append("容器里没有规则文件 %s" % rules_path)
    else:
        c = rules_change(rules_ours, unversion(r.stdout))
        if c:
            changes.append(c)
    if not changes:
        # The files are as this pack writes them; Prometheus may still run an earlier load (a reload that failed).
        url = ENV.get("PROMETHEUS_URL", "").rstrip("/")
        targets = http_json(url + "/api/v1/targets?state=active").get("data", {}).get("activeTargets", [])
        stale = []
        for job, addr in ((ENV.get("PULLER_JOB"), puller), (ENV.get("CENTRAL_JOB"), central + ":9598")):
            mine = [t for t in targets if (t.get("labels") or {}).get("job") == job]
            if not any("//%s/" % addr in (t.get("scrapeUrl") or "") for t in mine):
                stale.append("job %s" % job)
        groups = http_json(url + "/api/v1/rules").get("data", {}).get("groups", [])
        group = ENV.get("RULES_GROUP")
        loaded = sorted(x.get("name", "") for g_ in groups if g_.get("name") == group for x in g_.get("rules", []))
        if loaded != alert_names(rules_ours):
            stale.append("规则组 %s" % group)
        if stale:
            changes.append("文件已是这一版，Prometheus 还没有加载：%s（要重新加载）" % "、".join(stale))
    return ("INFO", summarize(changes)) if changes else ("OK", None)


def item_central(ctx):
    import render_vector as RV
    inv = PACK / "inventory.json"
    if not inv.is_file():
        raise Unavailable("没有 %s" % inv)
    try:
        with open(str(inv), encoding="utf-8") as f:
            model = str(json.load(f).get("model", "unknown"))   # as bin/central_up.sh passes MODEL_NAME
    except (OSError, ValueError) as e:
        raise Unavailable("inventory.json 读不了：%s" % e)
    r = run([sys.executable, str(SCRIPTS / "inventory_tool.py"), str(inv), "csv"])
    if r.returncode != 0:
        raise Unavailable("inventory.json 有问题：%s" % last_line(r.stderr))
    csv_ours = r.stdout
    yaml_ours = RV.build(dict(ENV, MODEL_NAME=model), "/var/lib/vector", "/etc/vector")
    write("central/central.yaml", yaml_ours, 0o600)
    write("central/inventory.csv", csv_ours, 0o644)
    name, image = ENV.get("CENTRAL_CONTAINER", ""), ENV.get("CENTRAL_IMAGE", "")
    info = docker_inspect(name)
    if info is None:
        return "INFO", "没有采集器容器 %s，会新建" % name
    cfg, host = info.get("Config") or {}, info.get("HostConfig") or {}
    mounts = {m.get("Destination"): m for m in info.get("Mounts") or []}
    changes = []
    if cfg.get("Image") != image:
        changes.append("镜像 %s，包里是 %s" % (cfg.get("Image"), image))
    else:
        r = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        if r.returncode != 0:
            changes.append("本机没有镜像 %s" % image)
        elif r.stdout.strip() != info.get("Image"):
            changes.append("镜像 %s 换过内容（容器用的不是现在这个 ID）" % image)
    conf = mounts.get("/etc/vector")
    if not conf:
        changes.append("容器没有挂 /etc/vector")
    else:
        d = conf.get("Source", "")
        texts = {}
        for fn in ("central.yaml", "inventory.csv"):
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    texts[fn] = f.read()
            except OSError:
                if not running(info):
                    raise Unavailable("读不了 %s/%s，容器也没在运行" % (d, fn))
                r = run(["docker", "exec", name, "cat", "/etc/vector/" + fn])
                if r.returncode != 0:
                    raise Unavailable("读不了 %s/%s：%s" % (d, fn, last_line(r.stderr)))
                texts[fn] = r.stdout
        c = text_change("central.yaml", unversion(yaml_ours), unversion(texts["central.yaml"]))
        if c:
            changes.append(c)
        if csv_ours != texts["inventory.csv"]:
            changes.append("inventory.csv（清单里的实例）")
        started = docker_started((info.get("State") or {}).get("StartedAt"))
        if running(info) and started is not None:
            for fn in ("central.yaml", "inventory.csv"):
                try:
                    # Same machine, same clock: a file the step wrote is always older than the start that followed.
                    if os.stat(os.path.join(d, fn)).st_mtime > started + 1:
                        changes.append("%s 在采集器启动以后改过，运行中的采集器读的是之前那一份" % fn)
                except OSError:
                    pass
    changes += central_args_changes(info, cfg, host, mounts, RV.RUN_SCRIPT)
    if changes:
        return "INFO", summarize(changes)
    if not running(info):
        return "OK", "容器没在运行，bin/central_up.sh 会直接启动它（不重建）"
    return "OK", None


def same_path(a, b):
    return os.path.realpath(a or "") == os.path.realpath(b or "")


def central_args_changes(info, cfg, host, mounts, run_script):
    """What differs from the docker run of bin/central_up.sh: entrypoint, command, network, restart policy, ulimit,
    the three mounts and CENTRAL_DOCKER_ARGS (through the label bin/central_up.sh sets)."""
    out = []
    if cfg.get("Entrypoint") != ["sh"] or cfg.get("Cmd") != ["-c", run_script]:
        out.append("容器的启动命令")
    if host.get("NetworkMode") != ENV.get("DOCKER_NETWORK"):
        out.append("网络 %s，包里是 %s" % (host.get("NetworkMode"), ENV.get("DOCKER_NETWORK")))
    if (host.get("RestartPolicy") or {}).get("Name") != "unless-stopped":
        out.append("重启策略 %s" % (host.get("RestartPolicy") or {}).get("Name"))
    if {"Name": "nofile", "Soft": 65536, "Hard": 65536} not in (host.get("Ulimits") or []):
        out.append("ulimit nofile")
    # The folder the collector mounts is the installed cluster's: the update script runs this from the new pack's
    # extraction with VLLM_PD_TARGET_DIR=<installed folder>, so that the preview does not report the mount as changed.
    target = pathlib.Path(os.environ.get("VLLM_PD_TARGET_DIR") or str(PACK))
    want = [("/etc/vector", "bind", str(target / "generated" / "central"), False)]
    if ENV.get("CLICKHOUSE_DATA_DIR"):
        want.append(("/var/lib/vector", "bind", os.path.join(ENV["CLICKHOUSE_DATA_DIR"], "vector"), True))
    else:
        want.append(("/var/lib/vector", "volume", ENV.get("CENTRAL_CONTAINER", "") + "-data", True))
    want.append(("/pull", "bind", ENV.get("PULL_DIR", ""), False))
    for dest, kind, src, rw in want:
        m = mounts.get(dest)
        ok = m is not None and m.get("Type") == kind and bool(m.get("RW")) == rw and (
            same_path(m.get("Source"), src) if kind == "bind" else m.get("Name") == src)
        if not ok:
            have = "没有" if m is None else (m.get("Source") if m.get("Type") == "bind" else m.get("Name"))
            out.append("挂载 %s：现场 %s，包里 %s" % (dest, have, src))
    label = (cfg.get("Labels") or {}).get(ARGS_LABEL)
    extra = ENV.get("CENTRAL_DOCKER_ARGS", "")
    if label is None:
        # A container made before this label existed: only an empty CENTRAL_DOCKER_ARGS can be taken as unchanged.
        if extra.strip():
            raise Unavailable("容器是旧版建的，看不出 CENTRAL_DOCKER_ARGS=%s 有没有生效" % extra)
    elif label != args_sha(extra):
        out.append("CENTRAL_DOCKER_ARGS")
    return out


def item_datasource(ctx):
    G, g = ctx.grafana()
    ours = G.datasource_body()
    write("datasource.json", json.dumps({k: v for k, v in ours.items() if k != "secureJsonData"}, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    code, live = g.get("/api/datasources/uid/" + G.DS_UID)
    if code == 404:
        return "INFO", "Grafana 里没有 UID 为 %s 的数据源，会新建" % G.DS_UID
    if code != 200:
        raise Unavailable("GET /api/datasources/uid/%s 返回 HTTP %d" % (G.DS_UID, code))
    write("datasource.live.json", json.dumps({k: v for k, v in live.items() if k not in ("secureJsonData",)}, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    changes = datasource_changes(ours, live)
    if changes:
        return "INFO", summarize(changes)
    # The password: Grafana's own test of the data source with what it keeps, then whether that is deploy.env's.
    code, health = g.get("/api/datasources/uid/%s/health" % G.DS_UID)
    if code in (404, 405, 501) or not isinstance(health, dict) or "status" not in health:
        raise Unavailable("Grafana 的数据源健康检查用不了（HTTP %d），密码比不了" % code)
    if code != 200 or health.get("status") != "OK":
        return "INFO", "密码或连接：Grafana 用它存的密码连不上 ClickHouse（%s）" % hide(str(health.get("message", ""))[:80])
    # Grafana's password works; it is deploy.env's when vllm_reader logs in with CH_READER_PASSWORD too (one password per
    # user). Only when Grafana reaches the local container; otherwise the health check is all there is.
    ch = ENV.get("CLICKHOUSE_CONTAINER", "")
    try:
        local = ENV.get("CLICKHOUSE_ADDR", ch) == ch and running(docker_inspect(ch))
    except Unavailable:
        local = False
    if local:
        try:
            ctx.ch.query("SELECT 1", user="vllm_reader", password=ENV.get("CH_READER_PASSWORD", ""))
        except Unavailable as e:
            return "INFO", "密码：deploy.env 的 CH_READER_PASSWORD 登不上 vllm_reader（%s）" % e
    return "OK", None


# What a PUT leaves at these values when the body does not name them.
DS_DEFAULTS = {"basicAuth": False, "basicAuthUser": "", "database": "", "url": "", "user": "", "withCredentials": False}
DS_IGNORED = {"id", "version", "readOnly", "orgId", "typeLogoUrl", "apiVersion", "secureJsonFields", "secureJsonData",
              "typeName", "access_control", "accessControl"}


def datasource_changes(ours, live):
    """Differences between the data source grafana_api.py writes and the live one (GET), secrets left out."""
    a = unversion_json({k: v for k, v in ours.items() if k != "secureJsonData"})
    b = unversion_json({k: v for k, v in live.items() if k not in DS_IGNORED})
    out = []
    show = lambda x: "没有" if x is MISSING else json.dumps(x, ensure_ascii=False)
    for k, v in a.items():
        if k == "jsonData":
            live_j = b.get("jsonData") or {}
            c = first_diff_path(v, live_j, "jsonData")
            if c:
                key = c.split(".", 1)[1] if "." in c else None
                if key and (key in v or key in live_j):
                    out.append("%s：现场 %s，包里 %s" % (c, show(live_j.get(key, MISSING)), show(v.get(key, MISSING))))
                else:
                    out.append(c)
        elif b.get(k, MISSING) != v:
            out.append("%s：现场 %s，包里 %s" % (k, show(b.get(k, MISSING)), show(v)))
    for k, v in DS_DEFAULTS.items():
        if k in b and b[k] != v:
            out.append("%s：现场 %s，写入后是 %s" % (k, show(b[k]), show(v)))
    if not (live.get("secureJsonFields") or {}).get("password"):
        out.append("没有存密码")
    return out


FUNCS = {"board": item_board, "ddl": item_ddl, "prometheus": item_prometheus, "central": item_central, "datasource": item_datasource}


def check(ctx, key):
    """(level, line) of one item."""
    label = LABEL[key]
    try:
        level, text = FUNCS[key](ctx)
    except Unavailable as e:
        return "WARN", "WARN %s：比不了（%s）" % (label, hide(str(e)))
    except Exception as e:   # a fault here must not stop a deploy step: the step then acts as v2.0 does
        return "WARN", "WARN %s：比不了（比对出错：%s：%s）" % (label, type(e).__name__, hide(str(e))[:120])
    if level == "OK":
        return "OK", "OK   %s：不变%s" % (label, "；" + text if text else "")
    return "INFO", "INFO %s：有变化：%s" % (label, hide(text))


def usage():
    print("用法：bin/compare.sh all|board|ddl|prometheus|central|datasource [--sql-out FILE]\n"
          "  FILE 要在 %s 下面，只能和 ddl 或 all 一起用" % OUT, file=sys.stderr)
    return 2


def main(argv):
    if not argv or argv[0] not in ["all"] + list(FUNCS):
        return usage()
    what, rest = argv[0], argv[1:]
    sql_out = None
    if rest:
        if len(rest) != 2 or rest[0] != "--sql-out" or what not in ("all", "ddl"):
            return usage()
        sql_out = pathlib.Path(os.path.realpath(rest[1]))
        if not str(sql_out).startswith(str(OUT) + os.sep):
            return usage()
    ctx = Ctx()
    try:
        ctx.tree()
        if sql_out:
            sql_out.parent.mkdir(parents=True, exist_ok=True)
            sql_out.write_text("", encoding="utf-8")
            ctx.sql_out = sql_out
    except SystemExit as e:
        print("错误：%s" % e if not str(e).startswith("错误") else e, file=sys.stderr)
        return 2
    except OSError as e:
        print("错误：写不了 %s：%s" % (sql_out, e), file=sys.stderr)
        return 2
    keys = [k for k, _ in ITEMS] if what == "all" else [what]
    counts = {"OK": 0, "INFO": 0, "WARN": 0}
    for k in keys:
        level, line = check(ctx, k)
        counts[level] += 1
        print(line, flush=True)
    if what == "all":
        print("RESULT: 不变 %d 项，有变化 %d 项，比不了 %d 项" % (counts["OK"], counts["INFO"], counts["WARN"]))
    return 0 if counts["OK"] == len(keys) else 10


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit as e:
        # names.py errors raised while importing grafana_api / prometheus_merge: deploy.env is wrong.
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            sys.exit(2)
        raise
