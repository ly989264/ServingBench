"""Unit tests of pack two v3's fixes of known issues 7, 8, 9, 10, 11 (3) and 14, the v3 update script and the
diagnostics changes (v3 plan, sections 3 and 4). No service is needed: ClickHouse, SSH and HTTP are replaced by the
values the tests give, the node scripts run in sh against a temporary folder instead of /proc.

    python3 -m pytest -q tests/v3
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACK = os.path.join(ROOT, "vllm-pd-grafana-panel2")
SCRIPTS = os.path.join(PACK, "scripts")
sys.dont_write_bytecode = True
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import criteria as C  # noqa: E402
import diag as D  # noqa: E402
import safety as S  # noqa: E402
import uninstall_old as U  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


KIT = load("diag_kit", os.path.join(ROOT, "diag-kit", "diag.py"))
UPD = load("update_pack2", os.path.join(ROOT, "update", "vllm_pd_update_pack2.py"))
PACK_SEGMENTS = open(os.path.join(PACK, "segments.json"), encoding="utf-8").read()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ==================== 14: keep_hours from deploy.env ====================
class TestKeepHours:
    def test_value(self):
        assert C.keep_hours_value("") == 12.0
        assert C.keep_hours_value(None) == 12.0
        assert C.keep_hours_value(" 24 ") == 24.0
        assert C.keep_hours_value("0.5") == 0.5
        for bad in ("0", "-3", "abc", "inf", "nan"):
            with pytest.raises(ValueError):
                C.keep_hours_value(bad)

    def test_text(self):
        assert C.hours_text(12.0) == "12"
        assert C.hours_text(0.5) == "0.5"

    def test_set_keeps_other_bytes(self):
        text = C.set_keep_hours(PACK_SEGMENTS, 36)
        assert json.loads(text)["line"]["keep_hours"] == 36
        assert text.replace('"keep_hours": 36', '"keep_hours": 12') == PACK_SEGMENTS
        assert C.set_keep_hours(text, 12) == PACK_SEGMENTS

    def test_set_needs_one_value(self):
        with pytest.raises(ValueError):
            C.set_keep_hours('{"line": {}}', 12)
        with pytest.raises(ValueError):
            C.set_keep_hours('{"keep_hours": 1, "x": {"keep_hours": 2}}', 12)

    @pytest.fixture
    def pack_copy(self, tmp_path):
        d = tmp_path / "pack"
        shutil.copytree(PACK, d, ignore=shutil.ignore_patterns("samples", "generated", "__pycache__", "offline"))
        return d

    def run_generate(self, d, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("NODE_LOG_KEEP_HOURS", "NODE_LOG_DIR_IN_CONTAINER", "SEGMENTS_FILE")}
        e.update(env, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, str(d / "scripts" / "generate.py")], env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)

    def test_generate_writes_the_key(self, pack_copy):
        p = self.run_generate(pack_copy, NODE_LOG_KEEP_HOURS="24", NODE_LOG_DIR_IN_CONTAINER="/data/logs")
        assert p.returncode == 0, p.stderr
        text = (pack_copy / "segments.json").read_text(encoding="utf-8")
        assert json.loads(text)["line"]["keep_hours"] == 24
        assert "keep_hours 改成 24" in p.stdout
        # every other byte is the pack's: the check's comparison gives the pack's sha back
        assert C.set_keep_hours(C.set_log_dir(text, S.PACK_LOG_DIR), C.PACK_KEEP_HOURS) == PACK_SEGMENTS

    def test_generate_blank_is_12(self, pack_copy):
        seg = pack_copy / "segments.json"
        seg.write_text(C.set_keep_hours(PACK_SEGMENTS, 48), encoding="utf-8")
        p = self.run_generate(pack_copy, NODE_LOG_KEEP_HOURS="")
        assert p.returncode == 0, p.stderr
        assert seg.read_text(encoding="utf-8") == PACK_SEGMENTS

    def test_generate_rejects_bad_value(self, pack_copy):
        p = self.run_generate(pack_copy, NODE_LOG_KEEP_HOURS="twelve")
        assert p.returncode != 0
        assert "NODE_LOG_KEEP_HOURS" in p.stderr
        assert (pack_copy / "segments.json").read_text(encoding="utf-8") == PACK_SEGMENTS

    def cluster(self, tmp_path, segments_text, version="v3", name="cluster"):
        d = tmp_path / name
        d.mkdir()
        (d / "segments.json").write_text(segments_text, encoding="utf-8")
        (d / "SHA256SUMS").write_text("%s  ./segments.json\n" % sha(PACK_SEGMENTS), encoding="utf-8")
        return types.SimpleNamespace(dir=str(d), version_file=version)

    def test_check_ignores_keep_hours_and_log_dir(self, tmp_path):
        text = C.set_keep_hours(C.set_log_dir(PACK_SEGMENTS, "/data/vllm-pd-diag/logs"), 6)
        o = S.Out()
        S.item_version_files(self.cluster(tmp_path, text), o, False)
        assert [lv for lv, _ in o.lines] == ["OK", "OK"], o.lines

    def test_check_still_sees_other_changes(self, tmp_path):
        text = C.set_keep_hours(PACK_SEGMENTS, 6).replace('"rotate_mib": 256', '"rotate_mib": 128')
        o = S.Out()
        S.item_version_files(self.cluster(tmp_path, text), o, False)
        assert o.lines[-1][0] == "FAIL" and "segments.json" in o.lines[-1][1]

    def test_pre_upgrade_accepts_v21(self, tmp_path):
        o = S.Out()
        S.item_version_files(self.cluster(tmp_path, PACK_SEGMENTS, "v2.1"), o, True)
        assert o.lines[0][0] == "OK", o.lines
        o = S.Out()
        S.item_version_files(self.cluster(tmp_path, PACK_SEGMENTS, "v2.0", name="old"), o, True)
        assert o.lines[0][0] == "FAIL"


# ==================== the v3 update script ====================
class TestUpdateScript:
    def test_versions(self):
        assert UPD.PACK_VERSION == "v3"
        assert UPD.FROM_VERSIONS == ("v2.1", "v3")
        assert UPD.PREV_VERSION == "v2.1"
        assert open(os.path.join(PACK, "VERSION")).read().strip() == UPD.PACK_VERSION
        filled = open(os.path.join(ROOT, "update", "vllm_pd_update_pack2_filled.py"), encoding="utf-8").read().splitlines()
        plain = open(os.path.join(ROOT, "update", "vllm_pd_update_pack2.py"), encoding="utf-8").read().splitlines()
        assert len(filled) == len(plain)
        assert [i for i, (a, b) in enumerate(zip(plain, filled)) if a != b] == [plain.index('USER_DIR = "/data/<用户目录>"')]

    def installed(self, tmp_path, keep=None, env_extra="", name="installed"):
        d = tmp_path / name
        shutil.copytree(os.path.join(PACK, "node"), d / "node")
        text = PACK_SEGMENTS if keep is None else C.set_keep_hours(PACK_SEGMENTS, keep)
        (d / "segments.json").write_text(text, encoding="utf-8")
        (d / "deploy.env").write_text("CLUSTER=glm53-1\nNODE_MODE=mock\n" + env_extra, encoding="utf-8")
        return str(d)

    def preview(self, tmp_path, installed, version="v2.1"):
        return UPD.migration_preview(installed, PACK, version, "mock", [], str(tmp_path / "preview"))

    def test_migrates_hand_edited_keep_hours(self, tmp_path):
        inst = self.installed(tmp_path, keep=24)
        plan = self.preview(tmp_path, inst)
        assert [(k, v) for k, v, _ in plan["migrate"]] == [("NODE_LOG_KEEP_HOURS", "24")]
        assert not [t for lv, t in plan["lines"] if "keep_hours" in t and lv == "WARN"]
        out = json.load(open(str(tmp_path / "preview" / "segments.json")))
        assert out["line"]["keep_hours"] == 24
        UPD.migrate.__globals__["NEW2"] = inst
        try:
            done = UPD.migrate(plan)
        finally:
            UPD.migrate.__globals__["NEW2"] = UPD.NEW2
        assert done and "NODE_LOG_KEEP_HOURS" in done[0]
        assert UPD.env_file_value(open(os.path.join(inst, "deploy.env")).read(), "NODE_LOG_KEEP_HOURS") == "24"
        # the second time nothing is written
        assert UPD.append_env(os.path.join(inst, "deploy.env"), [("NODE_LOG_KEEP_HOURS", "24")]) == []

    def test_no_migration_when_unchanged_or_set(self, tmp_path):
        assert self.preview(tmp_path, self.installed(tmp_path))["migrate"] == []
        inst = self.installed(tmp_path, keep=24, env_extra="NODE_LOG_KEEP_HOURS=6\n", name="rerun")
        plan = self.preview(tmp_path, inst, "v3")
        assert plan["migrate"] == []
        assert any(lv == "WARN" and "keep_hours = 24 → 6" in t for lv, t in plan["lines"])

    def test_other_keys_still_listed(self, tmp_path):
        inst = self.installed(tmp_path, keep=24)
        p = os.path.join(inst, "segments.json")
        text = open(p, encoding="utf-8").read().replace('"rotate_mib": 256', '"rotate_mib": 64')
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        lines = self.preview(tmp_path, inst)["lines"]
        assert any("line.rotate_mib" in t for _, t in lines)
        assert not any("line.keep_hours =" in t and "包里的值" in t for _, t in lines)

    def test_rollback_target_is_the_first_v21_backup(self, tmp_path, monkeypatch):
        root = tmp_path / "backups"
        for name, before in (("20261116-100000", "v2.1"), ("20261116-110000", "v3"), ("20261116-120000", "v2.1")):
            (root / name).mkdir(parents=True)
            json.dump({"cluster": UPD.CLUSTER, "pack": "包二 v3", "version_before": before, "made": "2026-11-16T%s" % name[9:]},
                      open(str(root / name / "manifest.json"), "w"))
        monkeypatch.setattr(UPD, "BACKUP_ROOT", str(root))
        assert UPD.prev_backup().endswith("20261116-100000")
        (root / "20261116-100000" / UPD.ROLLBACK_MARK).write_text("2026-11-16T11:30:00\n")
        assert UPD.prev_backup().endswith("20261116-120000")
        assert UPD.rollback_lines("")[0].startswith("回到 v2.1：")


class TestUpdateScriptClusters:
    """The v3 update script works on either cluster (--cluster), also on one without panel 1 (test)."""

    @pytest.fixture
    def site(self, tmp_path, monkeypatch):
        for k in ("CLUSTER", "NEW1", "NEW2", "MODEL_GROUP", "ROOT", "REGISTRY", "BACKUP_ROOT"):
            monkeypatch.setattr(UPD, k, getattr(UPD, k))
        root = tmp_path / "grafana"
        (root / "vllm-pd-grafana-panel1" / "glm53-1" / "bin").mkdir(parents=True)
        (root / "vllm-pd-grafana-panel1" / "glm53-1" / "bin" / "panel1_cluster.sh").write_text("")
        (root / "vllm-pd-grafana-panel2" / "test").mkdir(parents=True)
        reg = tmp_path / "registry.json"
        reg.write_text(json.dumps({"clusters": {"test": {"model_group": "GLM-test-folder", "panel2": {"dir": "x"}}}}))
        monkeypatch.setattr(UPD, "ROOT", str(root))
        monkeypatch.setattr(UPD, "REGISTRY", str(reg))
        monkeypatch.setattr(UPD, "BACKUP_ROOT", str(tmp_path / "backups"))
        return root

    def test_set_cluster(self, site):
        UPD.set_cluster("test")
        assert (UPD.CLUSTER, UPD.MODEL_GROUP) == ("test", "GLM-test-folder")
        assert UPD.NEW2 == str(site / "vllm-pd-grafana-panel2" / "test")
        assert not UPD.has_panel1()
        assert UPD.panel1_result() == (None, None)
        UPD.set_cluster("glm53-1")
        assert UPD.has_panel1()

    def test_no_panel1_skips_panel1_steps(self, site, monkeypatch):
        UPD.set_cluster("test")
        calls = []
        monkeypatch.setattr(UPD, "sh", lambda cmd, cwd=None, **kw: (calls.append((cmd, cwd)), (1, ""))[1])
        with pytest.raises(UPD.Stop):
            UPD.cluster_env()            # load_env fails in the fake folder; panel 1 is not looked at before
        assert all(cwd != UPD.NEW1 for _, cwd in calls)
        snap = {"containers": {}, "volumes": [], "registry": None, "old1": {}, "grafana": "g"}
        monkeypatch.setattr(UPD, "snapshot", lambda env: snap)
        monkeypatch.setattr(UPD, "installed_version", lambda folder: UPD.PACK_VERSION)
        calls.clear()
        results, p1 = UPD.invariants({}, {"snapshot": snap, "version_before": "v2.1"}, "更新")
        assert p1 == [] and not [t for lv, t in results if lv == "FAIL"]
        assert any(lv == "INFO" and "没有面板一" in t for lv, t in results)
        assert not any("panel1_cluster.sh" in c for c, _ in calls)

    def test_rollback_line_names_the_cluster(self, site, tmp_path):
        UPD.set_cluster("test")
        b = tmp_path / "backups" / "20261116-100000"
        b.mkdir(parents=True)
        json.dump({"cluster": "test", "pack": "包二 v3", "version_before": "v2.1", "made": "2026-11-16T10:00:00"}, open(str(b / "manifest.json"), "w"))
        line = UPD.rollback_lines(str(b))[0]
        assert line.startswith("回到 v2.1：") and line.endswith("rollback %s --cluster test" % b)

    def test_cluster_argument(self):
        for bad in (["check", "--cluster"], ["check", "--cluster", "Bad Name"]):
            p = subprocess.run([sys.executable, os.path.join(ROOT, "update", "vllm_pd_update_pack2.py")] + bad,
                               stdout=subprocess.PIPE, universal_newlines=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            assert p.returncode == 2 and "--cluster" in p.stdout


# ==================== run and rollback on a dirty site (11-16: test data never stops an update) ====================
BOARD, DS, GRAFANA_C = "vllm-pd-request-glm53-1", "vllm-pd-clickhouse-glm53-1", "site-grafana-ctr"
DATA_FLOW = ("panel2_cluster.sh check", "safety.py", " quality", "compare.sh", "nodes.sh status", "puller.sh status", "diag.sh")
SCENARIOS = ("chain_broken", "versions_mixed", "mock_stopped", "old_rows")


class FakeSite:
    """Grafana, ClickHouse and the shell of a monitoring machine for the update script, recording every call. Every
    data-flow probe answers FAIL, as a site with test data would: the chain is down, two filter versions write at
    once, the mock is stopped, the tables hold old rows."""

    def __init__(self, scenario, new2):
        self.scenario, self.new2 = scenario, new2
        self.sh_calls, self.ch_calls, self.grafana_calls = [], [], []
        self.board = {"uid": BOARD, "title": "面板二", "version": 7, "panels": []}

    def sh(self, cmd, cwd=None, timeout=3600, show_tail=0, log_output=True):
        self.sh_calls.append((cmd, cwd))
        if any(x in cmd for x in DATA_FLOW):
            return 1, "FAIL 链路通：拉取程序没有运行\nFAIL 过滤器版本：两个以上版本同时在写\nFAIL mock 进程 0 个，应为 2 个\nRESULT: FAIL（3 项）\n"
        if cmd.startswith("docker inspect --format '{{.State.Running}}'"):
            return 0, "true\n"
        if "bin/panel2_cluster.sh add" in cmd:
            # add copies the new pack into the folder: its VERSION becomes v3
            with open(os.path.join(self.new2, "VERSION"), "w") as f:
                f.write("v3\n")
            return 0, "已更新：表和视图：建表 phase_line\n跳过（内容不变）：数据源\n\n==== 步骤 18：升级安全检查不做（--no-safety-check）\nRESULT: OK\n"
        if "bin/panel1_cluster.sh check" in cmd:
            return 0, "RESULT: OK\n"
        if "bin/nodes.sh stop" in cmd:
            return 0, "OK   <地址> 没有 mock 进程在运行\nRESULT: OK\n"
        return 0, "RESULT: OK\n"

    def ch(self, env, sql, user="default"):
        self.ch_calls.append(sql)
        if "settings_profile_elements" in sql:
            return 0, "max_execution_time\t120", ""
        if sql.startswith("SHOW CREATE SETTINGS PROFILE"):
            return 0, "CREATE SETTINGS PROFILE vllm_reader_profile SETTINGS max_execution_time = 120", ""
        if "filter_selfcheck" in sql:
            mixed = self.scenario == "versions_mixed"
            return 0, "pd-diag-v2-2026-11-13@aaaa1111 pd-diag-v3-2026-11-16@bbbb2222" if mixed else "", ""
        if "FROM system.tables WHERE database = 'vllm_pd'" in sql:
            rows = 351000 if self.scenario == "old_rows" else 0
            return 0, "phase_line_cur\tView\t\\N\ndiag_event\tMergeTree\t%d\nphase_line\tMergeTree\t%d" % (rows, rows * 2), ""
        return 0, "", ""

    def grafana(self, env, method, path, body=None):
        self.grafana_calls.append((method, path, body))
        if path == "/api/user":
            return 200, {"orgId": 1}
        if path == "/api/user/orgs":
            return 200, [{"orgId": 1, "role": "Admin"}]
        if path == "/api/dashboards/uid/" + BOARD:
            return 200, {"dashboard": dict(self.board), "meta": {"folderUid": "glm"}}
        if path.startswith("/api/datasources/uid/"):
            return 200, {"uid": DS, "name": "vLLM ClickHouse（glm53-1）"}
        if path.startswith("/api/search"):
            return 200, []
        return 200, {}


def tree_hashes(root):
    out = {}
    for d, _, fs in os.walk(str(root)):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, str(root))] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


@pytest.mark.parametrize("scenario", SCENARIOS)
class TestRunOnDirtySite:
    @pytest.fixture
    def site(self, tmp_path, monkeypatch, scenario):
        for k in ("CLUSTER", "NEW1", "NEW2", "MODEL_GROUP", "ROOT", "REGISTRY", "BACKUP_ROOT", "LOG_DIR", "P2_NEW", "P2_PACK",
                  "SUMMARY", "LOG"):
            monkeypatch.setattr(UPD, k, getattr(UPD, k))
        root = tmp_path / "grafana"
        new1 = root / "vllm-pd-grafana-panel1" / "glm53-1"
        (new1 / "bin").mkdir(parents=True)
        (new1 / "bin" / "panel1_cluster.sh").write_text("#!/bin/bash\n")
        (new1 / "prometheus").mkdir()
        (new1 / "prometheus" / "inventory.json").write_text('{"prefill": {}}\n')
        new2 = root / "vllm-pd-grafana-panel2" / "glm53-1"
        shutil.copytree(os.path.join(PACK, "node"), str(new2 / "node"))
        (new2 / "bin").mkdir()
        (new2 / "bin" / "panel2_cluster.sh").write_text("#!/bin/bash\n")
        (new2 / "scripts").mkdir()
        shutil.copy(os.path.join(SCRIPTS, "criteria.py"), str(new2 / "scripts" / "criteria.py"))
        (new2 / "VERSION").write_text("v2.1\n")
        (new2 / "deploy.env").write_text("CLUSTER=glm53-1\nNODE_MODE=mock\n")
        (new2 / "inventory.json").write_text("{}\n")
        (new2 / "segments.json").write_text(PACK_SEGMENTS.replace(json.loads(PACK_SEGMENTS)["config_id"], "pd-diag-v2-2026-11-13"))
        reg = tmp_path / "registry.json"
        reg.write_text(json.dumps({"clusters": {"glm53-1": {"model_group": "GLM-5.3", "panel2": {"dir": str(new2)}}}}))
        monkeypatch.setattr(UPD, "ROOT", str(root))
        monkeypatch.setattr(UPD, "REGISTRY", str(reg))
        monkeypatch.setattr(UPD, "BACKUP_ROOT", str(tmp_path / "backups"))
        monkeypatch.setattr(UPD, "LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(UPD, "P2_NEW", str(tmp_path / "update"))
        monkeypatch.setattr(UPD, "P2_PACK", PACK)          # the new pack, already unpacked
        monkeypatch.setattr(UPD, "SUMMARY", [])
        (tmp_path / "update").mkdir()
        fake = FakeSite(scenario, str(new2))
        env = {"GRAFANA_URL": "http://g", "GRAFANA_USER": "admin", "GRAFANA_PASSWORD": "x", "GRAFANA_BOARD_UID": BOARD,
               "GRAFANA_DS_UID": DS, "CLICKHOUSE_CONTAINER": "vllm-pd-clickhouse-glm53-1", "CENTRAL_CONTAINER": "vllm-pd-vector-glm53-1",
               "PROMETHEUS_CONTAINER": "vllm-pd-prometheus-glm53-1", "NODE_MODE": "mock", "GRAFANA_CONTAINER": GRAFANA_C,
               "PANEL1_P_UID": "vllm-pd-prefill-glm53-1", "PANEL1_D_UID": "vllm-pd-decode-glm53-1", "CH_ADMIN_PASSWORD": "x",
               "CH_READER_PASSWORD": "x"}
        snap = {"containers": {}, "volumes": [], "registry": None, "old1": {}, "grafana": "g started"}
        for name, value in (("sh", fake.sh), ("ch", fake.ch), ("grafana", fake.grafana), ("cluster_env", lambda: dict(env)),
                            ("snapshot", lambda e: snap), ("verify_tars", lambda item: item("OK", "包的 sha256 正确")),
                            ("extract_pack", lambda item: item("OK", "新包已解开"))):
            monkeypatch.setattr(UPD, name, value)
        UPD.set_cluster("glm53-1")
        return fake, new1, new2

    def run_main(self, monkeypatch, *args):
        monkeypatch.setattr(sys, "argv", ["vllm_pd_update_pack2.py"] + list(args))
        monkeypatch.setattr(UPD, "SUMMARY", [])
        rc = UPD.main()
        if UPD.LOG:
            UPD.LOG.close()
        return rc

    def test_run_then_rollback(self, site, monkeypatch, capsys, scenario):
        fake, new1, new2 = site
        panel1_before = tree_hashes(new1)
        assert self.run_main(monkeypatch, "run", "--cluster", "glm53-1") == 0
        out = capsys.readouterr().out
        assert "vllm_pd_update_pack2.py run，包二 v3，集群 glm53-1，成功" in out
        # no data-flow check ran, and add was told not to run its own
        assert not [c for c, _ in fake.sh_calls if any(x in c for x in DATA_FLOW)], fake.sh_calls
        assert any("bin/panel2_cluster.sh add glm53-1" in c and "--no-safety-check" in c for c, _ in fake.sh_calls)
        # the cluster's data was listed with its rows and dropped, views first
        drops = [q for q in fake.ch_calls if q.startswith("DROP TABLE")]
        assert drops == ["DROP TABLE IF EXISTS vllm_pd.`phase_line_cur` SYNC", "DROP TABLE IF EXISTS vllm_pd.`diag_event` SYNC",
                         "DROP TABLE IF EXISTS vllm_pd.`phase_line` SYNC"]
        rows = 351000 if scenario == "old_rows" else 0
        assert "表 vllm_pd.diag_event：%d 行" % rows in out and "表 vllm_pd.phase_line：%d 行" % (2 * rows) in out
        # Grafana: read only during run; panel 1: its folder unchanged, only its read-only check ran there
        assert {m for m, _, _ in fake.grafana_calls} == {"GET"}
        assert all(c.startswith("bin/panel1_cluster.sh check") for c, cwd in fake.sh_calls if cwd == str(new1))
        assert not [c for c, _ in fake.sh_calls if GRAFANA_C in c or "restart" in c or "grafana_plugin" in c]
        assert tree_hashes(new1) == panel1_before

        backup = [x for x in out.splitlines() if x.startswith("备份：")][-1].split("：", 1)[1]
        fake.sh_calls.clear(), fake.ch_calls.clear(), fake.grafana_calls.clear()
        assert self.run_main(monkeypatch, "rollback", backup, "--cluster", "glm53-1") == 0
        out = capsys.readouterr().out
        assert "和更新前一致，校验全部通过" in out
        assert open(str(new2 / "VERSION")).read().strip() == "v2.1"
        assert not [c for c, _ in fake.sh_calls if any(x in c for x in DATA_FLOW)], fake.sh_calls
        assert len([q for q in fake.ch_calls if q.startswith("DROP TABLE")]) == 3
        writes = [(m, p, b) for m, p, b in fake.grafana_calls if m != "GET"]
        assert writes and all(p == "/api/dashboards/db" and b["dashboard"]["uid"] == BOARD for m, p, b in writes if m == "POST")
        assert all(p == "/api/datasources/uid/" + DS for m, p, _ in writes if m == "PUT")
        assert not [c for c, _ in fake.sh_calls if GRAFANA_C in c or "restart" in c]
        assert tree_hashes(new1) == panel1_before


def test_preflight_still_protects(tmp_path, monkeypatch):
    """What protects the machine stays a gate: a stopped ClickHouse container stops run before anything changes."""
    fake = FakeSite("chain_broken", str(tmp_path))
    monkeypatch.setattr(UPD, "sh", lambda cmd, cwd=None, **kw: (1, "false\n") if "State.Running" in cmd else fake.sh(cmd, cwd))
    assert UPD.container_running("vllm-pd-clickhouse-glm53-1") is False
    assert UPD.free_mb(str(tmp_path)) > 0


def test_drop_data_stops_when_clickhouse_cannot_list(monkeypatch):
    monkeypatch.setattr(UPD, "ch", lambda env, sql, user="default": (1, "", "Connection refused"))
    with pytest.raises(UPD.Stop):
        UPD.drop_data({}, "升级")


def test_add_can_leave_out_the_safety_check():
    sh_ = open(os.path.join(PACK, "bin", "panel2_cluster.sh"), encoding="utf-8").read()
    assert "--no-safety-check) nocheck=1; shift ;;" in sh_
    assert 'if [ "$m" = mock ] && [ -n "$nocheck" ]; then' in sh_
    assert subprocess.run(["bash", "-n", os.path.join(PACK, "bin", "panel2_cluster.sh")]).returncode == 0


def test_run_has_no_ignore_switch():
    p = subprocess.run([sys.executable, os.path.join(ROOT, "update", "vllm_pd_update_pack2.py"), "run", "--ignore-check"],
                       stdout=subprocess.PIPE, universal_newlines=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert p.returncode == 2


def panel1_out(down_jobs, panels=2):
    """Panel 1's check output as verify_panel1.py prints it, with these jobs down."""
    lines = ["== 1. scrape targets (up / down per job)"] + ["   DOWN %s: http://<地址>:<端口>/metrics  connection refused" % j for j in down_jobs]
    if not down_jobs and not panels:
        return "\n".join(lines + ["", "RESULT: OK"]) + "\n"
    return "\n".join(lines + ["", "RESULT: %d targets down, 0 missing, 0 instance problems, %d panels without data or with errors" % (
        len(down_jobs), panels)]) + "\n"


class TestPanel1Comparison:
    """invariants: panel 1's check after run or rollback against the one at backup time. This cluster's panel 2 jobs on
    panel 1's Prometheus (vllm-timing-puller/central-<cluster>) were just restarted and are down until the next
    scrape: they do not count as panel 1 getting worse."""
    BEFORE = ["vllm-decode"] * 15

    @pytest.fixture
    def site(self, tmp_path, monkeypatch):
        for k in ("CLUSTER", "NEW1", "NEW2", "MODEL_GROUP", "ROOT", "REGISTRY"):
            monkeypatch.setattr(UPD, k, getattr(UPD, k))
        root = tmp_path / "grafana"
        (root / "vllm-pd-grafana-panel1" / "glm53-1" / "bin").mkdir(parents=True)
        (root / "vllm-pd-grafana-panel1" / "glm53-1" / "bin" / "panel1_cluster.sh").write_text("")
        monkeypatch.setattr(UPD, "ROOT", str(root))
        monkeypatch.setattr(UPD, "REGISTRY", str(tmp_path / "none.json"))
        UPD.set_cluster("glm53-1")
        snap = {"containers": {}, "volumes": [], "registry": None, "old1": {}, "grafana": "g"}
        monkeypatch.setattr(UPD, "snapshot", lambda env: snap)
        monkeypatch.setattr(UPD, "installed_version", lambda folder: UPD.PACK_VERSION)
        return snap

    def judge(self, monkeypatch, snap, after_jobs):
        before = panel1_out(self.BEFORE)
        monkeypatch.setattr(UPD, "sh", lambda cmd, cwd=None, **kw: (1, before))
        result, counts = UPD.panel1_result()
        monkeypatch.setattr(UPD, "sh", lambda cmd, cwd=None, **kw: (1, panel1_out(after_jobs)))
        results, _ = UPD.invariants({}, {"snapshot": snap, "version_before": "v2.1", "panel1_check": result, "panel1_counts": counts}, "更新")
        return [(lv, t) for lv, t in results if "面板一 check" in t][0]

    def test_counts(self):
        UPD.set_cluster("glm53-1")
        assert UPD.panel1_counts(panel1_out([], panels=0)) == [0, 0, 0, 0]
        assert UPD.panel1_counts(panel1_out(self.BEFORE + ["vllm-timing-puller-glm53-1", "vllm-timing-central-glm53-1"])) == [15, 0, 0, 2]
        assert UPD.panel1_counts(panel1_out(["vllm-timing-puller-test"])) == [1, 0, 0, 2]
        assert UPD.panel1_counts("RESULT: 不认识的格式\n") is None

    @pytest.mark.parametrize("job", ["vllm-timing-puller-glm53-1", "vllm-timing-central-glm53-1"])
    def test_own_panel2_job_down_is_not_worse(self, site, monkeypatch, job):
        lv, text = self.judge(monkeypatch, site, self.BEFORE + [job])
        assert lv == "OK" and "不计本集群面板二的 2 个任务" in text
        # the line shows the counts it compared, not the raw 16
        assert "targets down 15（升级前 15）" in text and text.endswith("；相同") and "16" not in text

    def test_both_own_jobs_down(self, site, monkeypatch):
        assert self.judge(monkeypatch, site, self.BEFORE + ["vllm-timing-puller-glm53-1", "vllm-timing-central-glm53-1"])[0] == "OK"

    @pytest.mark.parametrize("job", ["vllm-prefill", "vllm-timing-puller-test"])
    def test_other_job_down_is_worse(self, site, monkeypatch, job):
        # one more of panel 1's own jobs, or another cluster's panel 2 job, down: panel 1 got worse
        assert self.judge(monkeypatch, site, self.BEFORE + [job])[0] == "FAIL"


class FakeClickHouse:
    """The cluster's database vllm_pd as system.tables lists it; DROP TABLE removes one. filter_file holds the file
    headers the filters wrote for P0 and D0 (api, engine and worker each)."""

    def __init__(self):
        self.tables = {"phase_line_cur": ("View", None), "request_events": ("View", None), "request_json": ("View", None),
                       "diag_event": ("MergeTree", 351), "phase_line": ("MergeTree", 702), "filter_file": ("MergeTree", 6),
                       "filter_other": ("MergeTree", 0), "filter_selfcheck": ("MergeTree", 40)}

    def ch(self, env, sql, user="default"):
        if "FROM system.tables WHERE database = 'vllm_pd'" in sql:
            order = sorted(self.tables.items(), key=lambda kv: (kv[1][0] != "View", kv[0]))
            return 0, "\n".join("%s\t%s\t%s" % (k, e, "\\N" if n is None else n) for k, (e, n) in order), ""
        if sql.startswith("DROP TABLE IF EXISTS vllm_pd.`"):
            self.tables.pop(sql.split("`")[1], None)
            return 0, "", ""
        return 0, "", ""


def test_drop_data_keeps_the_filter_records(monkeypatch, capsys):
    db = FakeClickHouse()
    monkeypatch.setattr(UPD, "ch", db.ch)
    line = UPD.drop_data({}, "升级")
    assert sorted(db.tables) == ["filter_file", "filter_other", "filter_selfcheck"]
    out = capsys.readouterr().out
    assert "保留 vllm_pd.filter_file：6 行" in out and "已删除 2 张表、3 个视图，共 1053 行" in out
    assert line.endswith("保留 filter_file、filter_other、filter_selfcheck")


def test_quality_after_run_still_sees_the_filter_files(monkeypatch, capsys):
    """vllm mode restarts no vLLM, so a filter does not write its file headers again: after drop_data the
    post-deployment check (part of quality) must still find every instance's api, engine and worker files."""
    db = FakeClickHouse()
    monkeypatch.setattr(UPD, "ch", db.ch)
    UPD.drop_data({}, "升级")
    for k, v in (("CLUSTER", "glm53-1"), ("NODE_MODE", "vllm"), ("MONITOR_HOST", "m"), ("PULLER_METRICS_PORT", "1")):
        monkeypatch.setenv(k, v)
    P = load("postdeploy_v3", os.path.join(SCRIPTS, "postdeploy.py"))
    headers = [{"instance": n, "roles": ["api", "engine", "worker"], "last": "2026-11-16 10:00:00"} for n in ("P0", "D0")]

    def pd_ch(sql):
        if "FROM vllm_pd.filter_file" in sql:
            return headers if "filter_file" in db.tables else []
        return []
    monkeypatch.setattr(P, "ch", pd_ch)
    monkeypatch.setattr(P, "load_inventory", lambda path: {"prefill": {"api_servers": ["<P>:7100"]}, "decode": {"api_servers": ["<D>:7100"]}})
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="0", stderr=""))
    monkeypatch.setattr(sys, "argv", ["postdeploy.py"])
    try:
        P.main()
    except Exception:  # noqa: BLE001 - later items need a live ClickHouse; the file items come first
        pass
    out = capsys.readouterr().out
    assert "OK    清单上的 2 个实例都有过滤器写的文件" in out
    assert "缺进程的文件" not in out and "没有过滤器写的文件" not in out


def test_missing_panel1_board_is_info():
    """Step 6 (check_cpu.sh) passes on a cluster without panel 1: a missing panel 1 dashboard only drops the jump at the
    top of the board, so the preflight says INFO, and check_cpu.sh prints INFO lines without counting them."""
    src = open(os.path.join(SCRIPTS, "grafana_api.py"), encoding="utf-8").read()
    pre = src[src.index("def preflight():"):src.index("def uninstall():")]
    assert 'print("INFO %s看板 %s 没找到' in pre and 'print("FAIL %s看板' not in pre
    sh_ = open(os.path.join(PACK, "bin", "check_cpu.sh"), encoding="utf-8").read()
    assert 'elif [ "$status" = INFO ]; then echo "INFO $rest"; else miss "$rest"; fi' in sh_


# ==================== 7: versions of processes that are still writing ====================
NOW = 1_800_000_000.0
ADDR_OF = {"P0": "10.0.0.1", "D0": "10.0.0.2"}


class TestVersions:
    def rows_after_restart(self):
        # P0 restarted 30 s ago: the old pid 100 wrote its last line 40 s ago with the old version, the new pid 200 has
        # no line yet. D0 runs on and already reports the new version.
        return [("old", "P0", 100, NOW - 170, NOW - 40), ("new", "D0", 300, NOW - 170, NOW - 5)]

    def test_restart_old_process_left_out(self):
        alive = {"10.0.0.1": [("P0", 200, NOW - 30)], "10.0.0.2": [("D0", 300, NOW - 3600)]}
        vs = C.live_versions(self.rows_after_restart(), alive, ADDR_OF)
        assert vs == [("new", "D0", 300)]
        starting = C.starting_instances(self.rows_after_restart(), alive, ADDR_OF, {a: NOW for a in alive}, 135)
        assert starting == ["P0"]
        out = C.judge_versions(vs, "new", "mock", 0, NOW, starting=starting)
        assert [v.level for v in out] == ["OK", "INFO"]
        assert "P0" in out[1].text

    def test_without_probe_old_rule(self):
        # the probe of P0's machine failed: the old process still counts, two versions are FAIL as in v2.1
        alive = {"10.0.0.2": [("D0", 300, NOW - 3600)]}
        vs = C.live_versions(self.rows_after_restart(), alive, ADDR_OF)
        assert sorted(vs) == [("new", "D0", 300), ("old", "P0", 100)]
        assert C.judge_versions(vs, "new", "mock", 0, NOW)[0].level == "FAIL"

    def test_real_old_process_still_fails(self):
        # pid 100 is still running with the old version: not a restart
        rows = [("old", "P0", 100, NOW - 170, NOW - 20), ("new", "D0", 300, NOW - 170, NOW - 5)]
        alive = {"10.0.0.1": [("P0", 100, NOW - 3600)], "10.0.0.2": [("D0", 300, NOW - 3600)]}
        vs = C.live_versions(rows, alive, ADDR_OF)
        assert sorted(vs) == [("new", "D0", 300), ("old", "P0", 100)]
        assert C.judge_versions(vs, "new", "mock", 0, NOW)[0].level == "FAIL"
        assert C.judge_versions(vs, "new", "vllm", 0, NOW)[0].level == "FAIL"

    def test_unknown_pid_is_kept(self):
        # pids from another namespace: the row's pid is not among the running ones, but no process started after it
        rows = [("old", "P0", 7, NOW - 170, NOW - 20)]
        alive = {"10.0.0.1": [("P0", 4242, NOW - 3600)]}
        assert C.live_versions(rows, alive, ADDR_OF) == [("old", "P0", 7)]

    def test_push_window_both_modes(self):
        vs = [("old", "P0", 100), ("new", "D0", 300)]
        for mode in ("mock", "vllm"):
            out = C.judge_versions(vs, "new", mode, NOW - 40, NOW)
            assert out[0].level == "INFO", (mode, out[0].text)
            assert C.judge_versions(vs, "new", mode, NOW - 200, NOW)[0].level == "FAIL"
        # a single old version right after a push (known issue 14 in mock mode)
        assert C.judge_versions([("old", "P0", 100)], "new", "mock", NOW - 30, NOW)[0].level == "INFO"
        # two versions, neither expected: FAIL even right after a push
        assert C.judge_versions([("a", "P0", 1), ("b", "D0", 2)], "new", "mock", NOW - 30, NOW)[0].level == "FAIL"

    def test_mock_no_rows(self):
        assert C.judge_versions([], "new", "mock", 0, NOW)[0].level == "FAIL"
        out = C.judge_versions([], "new", "mock", 0, NOW, starting=["P0"])
        assert [v.level for v in out] == ["INFO"]

    def test_starting_needs_young_process(self):
        alive = {"10.0.0.1": [("P0", 200, NOW - 400)]}
        assert C.starting_instances([], alive, ADDR_OF, {"10.0.0.1": NOW}, 135) == []

    def test_node_probe_lists_mock_processes(self, tmp_path):
        proc = tmp_path / "proc"
        (proc / "4711").mkdir(parents=True)
        (proc / "uptime").write_text("1000.00 5000.00\n")
        fields = ["S"] + ["0"] * 18 + ["50000"] + ["0"] * 10
        (proc / "4711" / "stat").write_text("4711 (python3) " + " ".join(fields) + "\n")
        (proc / "4711" / "cmdline").write_bytes(b"\0".join([b"python3", b"pd032_mock.py", b"run-instance", b"--instance", b"D3", b"--port", b"9000"]) + b"\0")
        bin_ = tmp_path / "bin"
        bin_.mkdir()
        (bin_ / "pgrep").write_text("#!/bin/sh\n[ \"$1\" = -f ] && echo 4711\nexit 0\n")
        os.chmod(str(bin_ / "pgrep"), 0o755)
        script = "RAW_C=/nonexistent\nRAW_L=/nonexistent\n" + S.NODE_PROBE.replace("/proc/", str(proc) + "/")
        p = subprocess.run(["sh", "-s"], input=script, stdout=subprocess.PIPE, universal_newlines=True,
                           env={"PATH": "%s:%s" % (bin_, os.environ["PATH"]), "HOME": str(tmp_path)})
        lines = [x.split("\t") for x in p.stdout.splitlines()]
        alive = [x[1:] for x in lines if x[0] == "alive"]
        assert alive == [["4711", "50000", "D3"]]
        now = int([x[1] for x in lines if x[0] == "now"][0])
        clock = [x[1:] for x in lines if x[0] == "clock"][0]
        got = S.mock_alive(now, clock, alive)
        hz = float(clock[1])
        assert got == [("D3", 4711, now - 1000.0 + 50000 / hz)]

    def test_item_filter_wiring(self, monkeypatch):
        c = types.SimpleNamespace(mode="mock", version="new", line={}, instances={"10.0.0.1": [("P0", "8000")], "10.0.0.2": [("D0", "9000")]},
                                  get=lambda k, d="": d)
        rows = [{"v": "old", "instance": "P0", "pid": 100, "f": NOW - 170, "l": NOW - 40},
                {"v": "new", "instance": "D0", "pid": 300, "f": NOW - 170, "l": NOW - 5}]
        monkeypatch.setattr(S, "ch", lambda c_, sql, timeout=60: (rows, None))
        monkeypatch.setattr(S.time, "time", lambda: NOW)
        facts = {"10.0.0.1": {"now": int(NOW), "pushed": int(NOW - 3600), "alive": [("P0", 200, NOW - 30)]},
                 "10.0.0.2": {"now": int(NOW), "pushed": int(NOW - 3600), "alive": [("D0", 300, NOW - 3600)]}}
        o = S.Out()
        S.item_filter(c, o, facts, None)
        assert [lv for lv, _ in o.lines] == ["OK", "INFO"], o.lines


# ==================== 10: an idle vllm cluster ====================
class TestIdleChain:
    def test_rule(self):
        assert C.judge_idle_chain({"P0": 150, "D0": 200}, 135, 135).level == "WARN"
        v = C.judge_idle_chain({"P0": 150, "D0": 500}, 135, 135)
        assert v.level == "FAIL" and "D0" in v.text
        assert C.judge_idle_chain({"P0": 150, "D0": None}, 135, 135).level == "FAIL"
        assert C.judge_idle_chain({}, 135, 135).level == "FAIL"

    def cluster(self):
        return types.SimpleNamespace(mode="vllm", version="v@1", line={}, instances={"10.0.0.1": [("P0", "8000")], "10.0.0.2": [("D0", "9000")]},
                                     get=lambda k, d="": {"MONITOR_HOST": "m", "PULLER_METRICS_PORT": "1", "PULLER_JOB": "a",
                                                          "CENTRAL_JOB": "b", "PULL_INTERVAL_S": "10"}.get(k, d))

    def run_chain(self, monkeypatch, selfcheck_age, puller_age=5):
        monkeypatch.setattr(S, "http_json", lambda url, timeout=10, auth=None: (200, [{"addr": "10.0.0.1", "age": puller_age}], None))
        monkeypatch.setattr(S, "prom", lambda c, q: ([{"value": [0, "1"]}], None))

        def ch(c, sql, timeout=60):
            if "dateDiff" in sql:
                return [{"instance": "P0", "a": selfcheck_age}, {"instance": "D0", "a": selfcheck_age}], None
            if "uniqExact" in sql:
                return [{"n": 0}], None
            return [{"s": 0, "l": 0}], None
        monkeypatch.setattr(S, "ch", ch)
        o = S.Out()
        S.item_chain(self.cluster(), o, {"P0": "records", "D0": "records"})
        return o.lines

    def test_idle_is_warn(self, monkeypatch):
        lines = self.run_chain(monkeypatch, 200)
        assert not [t for lv, t in lines if lv == "FAIL"], lines
        assert any(lv == "WARN" and "最近没有请求" in t for lv, t in lines)

    def test_stale_selfcheck_is_fail(self, monkeypatch):
        lines = self.run_chain(monkeypatch, 900)
        assert any(lv == "FAIL" and "过滤器停了" in t for lv, t in lines)

    def test_puller_failing_is_fail(self, monkeypatch):
        lines = self.run_chain(monkeypatch, 200, puller_age=500)
        assert any(lv == "FAIL" and "最近 135 秒没有这一版的行" in t for lv, t in lines)


# ==================== 9: diag vllm, environment of the main process ====================
INSIDE_ENV = "VLLM_LOGGING_CONFIG_PATH=/opt/vllm-pd-diag/pd_diag_logging.json"


@pytest.mark.parametrize("mod", [D, KIT], ids=["pack", "kit"])
class TestDiagVllm:
    def test_pythonpath_trailing_slash(self, mod):
        assert mod.in_pythonpath("/opt/vllm-pd-diag", "/x:/opt/vllm-pd-diag/")
        assert mod.in_pythonpath("/opt/vllm-pd-diag/", "/opt/vllm-pd-diag")
        assert not mod.in_pythonpath("/opt/vllm-pd-diag", "/opt/vllm-pd-diag-old:/x")
        assert not mod.in_pythonpath("/opt/vllm-pd-diag", None)

    def test_multiline_command_line(self, mod, tmp_path):
        proc = tmp_path / "proc"
        d = proc / "321"
        d.mkdir(parents=True)
        (proc / "uptime").write_text("1000.00 5000.00\n")
        (d / "stat").write_text("321 (python3) S 1 " + " ".join(["0"] * 17) + " 777 0\n")
        (d / "status").write_text("Uid:\t1000\t1000\t1000\t1000\nNSpid:\t321\n")
        (d / "comm").write_text("python3\n")
        cfg = '{"a": 1,\n "b":\t2}'
        (d / "cmdline").write_bytes(b"\0".join([b"vllm", b"serve", b"/m", b"--port", b"8000", b"--kv-transfer-config", cfg.encode()]) + b"\0")
        (d / "environ").write_bytes(("%s\0PYTHONPATH=/opt/vllm-pd-diag/\0HOME=/root\0" % INSIDE_ENV).encode())
        script = "CONF_IN=/opt/vllm-pd-diag\n" + mod.INSIDE.replace("/proc", str(proc))
        out = subprocess.run(["sh", "-s"], input=script, stdout=subprocess.PIPE, universal_newlines=True).stdout
        # the proc line as both tools read it: proc, pid, ppid, uid, start, comm, readable, cmd, env (\001-separated)
        procs = [x.split("\t") for x in out.splitlines() if x.startswith("proc\t")]
        assert len(procs) == 1 and len(procs[0]) >= 9, out
        procs = [{"cmd": x[7], "env": dict(kv.split("=", 1) for kv in x[8].split("\001") if "=" in kv)} for x in procs]
        if mod is D:
            assert mod.parse_inside(out.splitlines())[1][0]["env"] == procs[0]["env"]
        assert procs[0]["env"].get("VLLM_LOGGING_CONFIG_PATH") == INSIDE_ENV.split("=", 1)[1]
        assert "--port 8000" in procs[0]["cmd"]
        assert mod.in_pythonpath("/opt/vllm-pd-diag", procs[0]["env"].get("PYTHONPATH"))


# ==================== diag log: 9 kinds, rc1 profiles, shared pool tasks ====================
def write_log(path, records):
    with open(str(path), "w", encoding="utf-8") as f:
        for o in records:
            f.write("2026-11-16 10:00:00 INFO (EngineCore_DP0 pid=1) [PD_DIAG] %s\n" % json.dumps(o))


SEVEN = [{"event": "api_summary", "scope": "http"}, {"event": "api_summary", "scope": "engine_submission"},
         {"event": "api_summary", "scope": "request"}, {"event": "core_summary", "scope": "request"},
         {"event": "core_summary", "scope": "execution"}, {"event": "worker_summary", "phase": "execute"},
         {"event": "worker_summary", "phase": "sample"}]


@pytest.mark.parametrize("mod", [D, KIT], ids=["pack", "kit"])
class TestDiagLog:
    def test_kinds_p_side(self, mod, tmp_path):
        write_log(tmp_path / "P.log", SEVEN + [{"event": "pool_task", "scope": "task", "request_id": "r1"}])
        out = mod.judge_kinds(mod.profile_of(str(tmp_path / "P.log")))
        assert out[0][0] == "OK" and "9 种" in out[0][1]
        levels = {t.split("：")[1].split(" ")[0]: lv for lv, t in out[1:]}
        assert levels == {"pool_task[task]": "OK", "pd_transfer_summary[request_generation]": "INFO"}

    def test_kinds_old_log(self, mod, tmp_path):
        write_log(tmp_path / "old.log", SEVEN)
        out = mod.judge_kinds(mod.profile_of(str(tmp_path / "old.log")))
        assert [lv for lv, _ in out] == ["OK", "WARN"]
        write_log(tmp_path / "bad.log", SEVEN[1:])
        assert mod.judge_kinds(mod.profile_of(str(tmp_path / "bad.log")))[0][0] == "FAIL"

    def test_shared_pool_tasks(self, mod, tmp_path):
        write_log(tmp_path / "P.log", [
            {"event": "pool_task", "scope": "task", "request_ids": ["a", "b"]},
            {"event": "pool_task", "scope": "task", "request_ids": ["a"]},
            {"event": "pool_task", "scope": "task", "request_id": "a", "request_ids": ["a", "b"]},
            {"event": "pool_task", "scope": "task", "request_ids": []}])
        assert mod.profile_of(str(tmp_path / "P.log"))["shared_pool_tasks"] == 1

    def test_profiles_and_pick(self, mod, tmp_path):
        base_dir = os.path.join(os.path.dirname(mod.__file__), "profiles") if mod is KIT else os.path.join(PACK, "samples", "profiles")
        bases = mod.known_profiles(base_dir)
        assert [n for n, _ in bases] == ["rc1-2026-11-16-D.json", "rc1-2026-11-16-P.json", "server-2026-11-12.json"]
        write_log(tmp_path / "P.log", SEVEN + [{"event": "pool_task", "scope": "task"}, {"event": "pd_transfer_summary", "scope": "link"}])
        assert mod.pick_profile(mod.profile_of(str(tmp_path / "P.log")), bases)[0] == "rc1-2026-11-16-P.json"
        write_log(tmp_path / "old.log", SEVEN)
        assert mod.pick_profile(mod.profile_of(str(tmp_path / "old.log")), bases)[0] == "server-2026-11-12.json"

    def test_empty_key_line(self, mod):
        rows = [{"instance": "P0", "pid": 1, "k": "73"}, {"instance": "P0", "pid": 2, "k": "2"}, {"instance": "D0", "pid": 3, "k": "0"}]
        text = mod.empty_key_line(rows)
        assert "P0 75 条" in text and "D0" not in text
        assert mod.empty_key_line([{"instance": "D0", "pid": 3, "k": 0}]).startswith("过滤器丢掉的空键记录：0 条")


def test_rc1_profiles_match_counts():
    """The two rc1 profiles are the same file in the pack and in the kit and hold counts only."""
    for side, kinds in (("P", "pool_task[task]"), ("D", "pd_transfer_summary[request_generation]")):
        a = open(os.path.join(PACK, "samples", "profiles", "rc1-2026-11-16-%s.json" % side), "rb").read()
        b = open(os.path.join(ROOT, "diag-kit", "profiles", "rc1-2026-11-16-%s.json" % side), "rb").read()
        assert a == b
        prof = json.loads(a)
        assert prof["kinds"].get(kinds)
        assert prof["shared_pool_tasks"] == 0
        assert set(prof) == {"lines", "pd_diag", "mooncake", "procs", "kinds", "fields", "stages", "bad_json", "shared_pool_tasks", "name"}


# ==================== 11 (3): vllm-mode purge keeps the file being written ====================
class TestPurgeVllm:
    def test_keeps_highest_seq(self, tmp_path):
        logs = tmp_path / "logs"
        files = ["P0/api.20261116T100000.123.0001.log", "P0/api.20261116T100000.123.0002.log",
                 "P0/engine.20261116T100000.124.0001.log", "P0/worker.20261116T090000.99.0003.log",
                 "P0/worker.20261116T090000.99.0010.log", "D0/pool.20261116T100000.200.0001.log", "D0/other.txt"]
        for f in files:
            (logs / f).parent.mkdir(parents=True, exist_ok=True)
            (logs / f).write_text("x\n")
        (logs / "empty").mkdir()
        p = subprocess.run(["sh", "-s"], input="L=%s\n" % logs + U.VLLM_LOG_CLEAN, stdout=subprocess.PIPE, universal_newlines=True)
        left = sorted(os.path.relpath(os.path.join(d, f), str(logs)) for d, _, fs in os.walk(str(logs)) for f in fs)
        assert left == ["D0/other.txt", "D0/pool.20261116T100000.200.0001.log", "P0/api.20261116T100000.123.0002.log",
                        "P0/engine.20261116T100000.124.0001.log", "P0/worker.20261116T090000.99.0010.log"]
        assert "kept\t2\t4" in p.stdout
        assert not (logs / "empty").exists()

    def test_remove_uses_it_only_in_vllm_mode(self):
        src = open(os.path.join(SCRIPTS, "uninstall_old.py"), encoding="utf-8").read()
        assert 'if o.version == "a87f4d74" and o.node_mode == "vllm":\n' in src
        assert "VLLM_LOG_CLEAN" in src.split('if o.version == "a87f4d74" and o.node_mode == "vllm":\n', 1)[1].split("elif", 1)[0]


# ==================== 8: the data source variable ====================
def test_datasource_variable_skips_url():
    style = json.load(open(os.path.join(PACK, "grafana", "board_style.json"), encoding="utf-8"))
    ds = [v for v in style["templating"]["list"] if v.get("type") == "datasource"]
    assert len(ds) == 1 and ds[0]["skipUrlSync"] is True and ds[0]["hide"] == 2


def test_generated_board_keeps_skip_url_sync(tmp_path):
    d = tmp_path / "pack"
    shutil.copytree(PACK, d, ignore=shutil.ignore_patterns("samples", "generated", "__pycache__", "offline"))
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_LOG_KEEP_HOURS", "NODE_LOG_DIR_IN_CONTAINER", "SEGMENTS_FILE")}
    p = subprocess.run([sys.executable, str(d / "scripts" / "generate.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert p.returncode == 0, p.stderr
    board = json.load(open(str(d / "generated" / "vllm-pd-request.json"), encoding="utf-8"))
    ds = [v for v in board["templating"]["list"] if v.get("type") == "datasource"]
    assert ds[0].get("skipUrlSync") is True


# ==================== A's findings: the processes of a request, the format check, the node self-check ====================
import gzip  # noqa: E402

import generate as G  # noqa: E402
import node_selfcheck as N  # noqa: E402

FMT_PACK = load("fmt_pack", os.path.join(PACK, "tools", "check_pd_diag_format.py"))
FMT_KIT = load("fmt_kit", os.path.join(ROOT, "diag-kit", "tools", "check_pd_diag_format.py"))


def test_request_processes_are_three():
    """generate.py: the single-request JSON flags a side whose api, engine or worker wrote nothing; the background save
    task (proc save, from pool_task) is not counted, so a request the cache pool saved for is not "4 of 3"."""
    assert G.REQUEST_PROCS == ("api", "engine", "worker")


def test_generated_sql_counts_three_processes(tmp_path):
    d = tmp_path / "pack"
    shutil.copytree(PACK, d, ignore=shutil.ignore_patterns("samples", "generated", "__pycache__", "offline"))
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_LOG_KEEP_HOURS", "NODE_LOG_DIR_IN_CONTAINER", "SEGMENTS_FILE")}
    p = subprocess.run([sys.executable, str(d / "scripts" / "generate.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert p.returncode == 0, p.stderr
    views = (d / "generated" / "views.sql").read_text(encoding="utf-8")
    counted = "length(arrayFilter(x -> has(['api', 'engine', 'worker'], x), arrayDistinct(arrayFlatten(groupArray(procs))))) AS lines"
    assert views.count(counted) == 2            # the P and the D view
    assert "length(arrayDistinct(arrayFlatten(groupArray(procs)))) AS lines" not in views
    assert views.count("' of 3 processes'") == 2


def test_quality_counts_three_processes():
    """verify_panel2.py (bin/panel2_cluster.sh quality): a request is complete when api, engine and worker wrote on each
    side; the save task is not one of them, so "p = 3" holds for a request the cache pool saved for."""
    src = open(os.path.join(SCRIPTS, "verify_panel2.py"), encoding="utf-8").read()
    assert "uniqExactIf(proc, role = 'prefill' AND proc IN ('api', 'engine', 'worker')) AS p" in src
    assert "uniqExactIf(proc, role = 'decode' AND proc IN ('api', 'engine', 'worker')) AS d" in src
    assert "uniqExactIf(proc, role = 'prefill') AS p" not in src


def record(event, scope, **kw):
    o = {"event": event, "version": 2, "host": "h", "pid": 1, "native_tid": 1, "run_id": "r", "timestamp_ns": 1,
         "record_id": "x", "status": "ok", "stages": {}, "scope": scope}
    o.update(kw)
    return o


def problems(mod, o):
    got = []
    mod.check_record(o, lambda code, detail: got.append((code, detail)))
    return got


POOL = dict(request_id=None, operation="save_submit", duration_ns=5, start_ns=1, end_ns=6, tp_rank=0)


@pytest.mark.parametrize("mod", [FMT_PACK, FMT_KIT], ids=["pack", "kit"])
class TestFormatCheck:
    def test_eleven_kinds(self, mod):
        assert len(mod.KINDS) == 11
        for k in (("pool_task", "task"), ("pd_transfer_summary", "task"), ("pd_transfer_summary", "request_generation"),
                  ("pd_transfer_summary", "link")):
            assert k in mod.KINDS

    def test_pool_task(self, mod):
        skipped = {st: {"status": "skipped"} for st in ("build_key", "lookup", "prepare", "sync", "put")}
        assert problems(mod, record("pool_task", "task", stages=skipped, **POOL)) == []
        unmeasured = dict(skipped, put={"status": "unmeasured"})
        assert problems(mod, record("pool_task", "task", stages=unmeasured, **POOL)) == []
        timed = dict(skipped, put={"status": "ok", "duration_ns": 4166000, "start_ns": 10, "end_ns": 4166010, "calls": 1})
        assert problems(mod, record("pool_task", "task", stages=timed, **POOL)) == []
        ran_untimed = dict(skipped, put={"status": "ok"})
        assert ("E5", "pool_task[task].stages.put.duration_ns") in problems(mod, record("pool_task", "task", stages=ran_untimed, **POOL))
        assert ("E6", "pool_task[task].operation") in problems(mod, record("pool_task", "task", **{k: v for k, v in POOL.items() if k != "operation"}))
        failed = dict(skipped, put={"status": "failed", "duration_ns": None})
        assert [c for c, _ in problems(mod, record("pool_task", "task", stages=failed, **POOL))] == ["W2"]

    def test_transfer(self, mod):
        rg = record("pd_transfer_summary", "request_generation", request_id="r1", transfer_envelope_ns=155901000)
        del rg["stages"]
        assert problems(mod, rg) == []
        assert ("E3", "stages") in problems(mod, {k: v for k, v in record("pd_transfer_summary", "link", request_id="r1").items() if k != "stages"})
        task = record("pd_transfer_summary", "task", request_id="r1",
                      stages={"wait_for_transfer": {"status": "succeeded", "duration_ns": 2633000, "start_ns": 1, "end_ns": 2633001}})
        assert problems(mod, task) == []
        assert problems(mod, record("pd_transfer_summary", "link", request_id="r1")) == []
        assert [c for c, _ in problems(mod, record("pd_transfer_summary", "other"))] == ["E2"]

    def test_patch_01_kinds_unchanged(self, mod):
        # a step record still needs its stages
        assert ("E5", "core_summary[execution].stages.lookup") in problems(
            mod, record("core_summary", "execution", execution_id="e", request_ids=[]))

    def test_samples_pass(self, mod):
        out = subprocess.run([sys.executable, "-I", mod.__file__, os.path.join(PACK, "samples", "pd032_full_P0_D0.log.gz")],
                             stdout=subprocess.PIPE, universal_newlines=True)
        assert out.returncode == 0 and out.stdout.rstrip().endswith("RESULT: OK"), out.stdout[-400:]


def test_format_check_copies_identical():
    assert open(FMT_PACK.__file__, "rb").read() == open(FMT_KIT.__file__, "rb").read()


class TestNodeSelfcheck:
    def test_expected_roles(self):
        assert N.expected_roles(False) == ["api", "engine", "worker"]
        assert N.expected_roles(True) == ["api", "engine", "pool", "worker"]

    def run_selfcheck(self, tmp_path, segments, sample):
        conf = tmp_path / "conf"
        conf.mkdir()
        for f in ("pd_diag_logfilter.py", "pd_diag_logging.json"):
            shutil.copy(os.path.join(PACK, "node", f), str(conf / f))
        (conf / "segments.json").write_text(json.dumps(segments), encoding="utf-8")
        (tmp_path / "logs").mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PD_DIAG_MAPPING", "PD_DIAG_LOG_DIR")}
        env.update(VLLM_LOGGING_CONFIG_PATH=str(conf / "pd_diag_logging.json"), PYTHONPATH=str(conf), PD_DIAG_INSTANCE="selfcheck",
                   PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "node_selfcheck.py"), "--sample", str(sample),
                            "--truth", os.path.join(PACK, "samples", "pd032_truth_P0_D0.jsonl"), "--log-dir", str(tmp_path / "logs")],
                           env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        return [x for x in p.stdout.splitlines() if "样本" in x or x.startswith("RESULT")]

    @staticmethod
    def written(line):
        """(written, expected) of the self-check's sample line: 「请求级记录写出 X / 应有 Y」."""
        m = re.search(r"请求级记录写出 (\d+) / 应有 (\d+)", line)
        assert m, line
        return int(m.group(1)), int(m.group(2))

    def test_pack_sample(self, tmp_path):
        out = self.run_selfcheck(tmp_path, json.loads(PACK_SEGMENTS), os.path.join(PACK, "samples", "pd032_full_P0_D0.log.gz"))
        assert out[-1] == "RESULT: OK", out

    def test_pool_records(self, tmp_path):
        seg = json.loads(PACK_SEGMENTS)
        seg["roles"]["by_event_prefix"].append({"prefix": "pool_task", "role": "pool"})
        seg["roles"]["ranked_roles"] = ["worker", "pool"]
        save = seg["sides"]["prefill"]["background"][0]
        save["src"] = [{"event": "pool_task", "where": {"scope": "task", "operation": "save"}, "field": "duration_ns"}]
        sample = tmp_path / "sample.log.gz"
        pool = [dict(record("pool_task", "task", request_id="pool-r%d" % i, operation="save", duration_ns=5, start_ns=1, end_ns=6),
                     tp_rank=rank) for i, rank in ((1, 0), (2, 0), (3, 1))]
        with gzip.open(os.path.join(PACK, "samples", "pd032_full_P0_D0.log.gz"), "rt", encoding="utf-8") as src, \
                gzip.open(str(sample), "wt", encoding="utf-8") as dst:
            dst.write(src.read())
            for o in pool:
                # compact JSON, as the patch writes it: the filter finds "event" and "tp_rank" by a string scan
                dst.write("(EngineCore_DP0 pid=4711) INFO 11-16 10:00:00 [pd_diagnostics.py:156] [PD_DIAG] %s\n" % json.dumps(
                    o, separators=(",", ":")))
        (tmp_path / "base").mkdir()
        base = self.run_selfcheck(tmp_path / "base", seg, os.path.join(PACK, "samples", "pd032_full_P0_D0.log.gz"))
        (tmp_path / "pool").mkdir()
        out = self.run_selfcheck(tmp_path / "pool", seg, sample)
        assert out[-1] == "RESULT: OK", out
        assert "文件 api engine pool worker" in out[0]
        # whatever the pack's sample holds: the same records plus the two rank-0 pool records, all of them written
        n = self.written(base[0])[1]
        assert self.written(out[0]) == (n + 2, n + 2), (base[0], out[0])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
