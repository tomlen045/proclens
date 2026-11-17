#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proclens — 进程生命周期透镜（zero-dependency single-file CLI）

回答三个没人盯的问题：
  1. 谁是僵尸 —— 子进程死了，父进程不收尸
  2. 谁刚变成孤儿 —— 父进程先走一步，进程被 reparent 到 pid 1
  3. 谁在反复横死 —— 同名进程一轮轮出生即死（crash loop / fork 风暴苗头）

哨兵套件定位：porteye 盯「谁在听」/ authguard 盯「谁想进来」/ callhome 盯「谁在偷偷
打电话回家」/ proclens 盯「谁在反复横死」。

零依赖：Python 3.8+ 标准库，ps 一条命令取数，拷走就能跑。
退出码：0 干净 / 1 用法错 / 2 有 WARN / 3 有 CRIT / 4 内部错误。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

VERSION = "1.0.0"
EXIT_OK, EXIT_USAGE, EXIT_WARN, EXIT_CRIT, EXIT_INTERNAL = 0, 1, 2, 3, 4

PS_CMD = ["ps", "-eo", "pid=,ppid=,user=,etime=,state=,pcpu=,rss=,command="]

# 默认豁免：系统级批量短命工作者 + shell 工具箱（脚本天生高频短命，churn 是正常形态）。
# 注意：不放 "worker"/"runner" 这类通用词——会把真实应用的 crash loop 藏掉。
DEFAULT_LOOP_WHITELIST = {
    "mdworker", "mdworker_shared", "mds", "mds_stores",
    "launchd", "init", "systemd", "ps",
    "sleep", "sh", "bash", "zsh", "git", "curl", "grep", "awk", "sed",
    "cat", "tail", "head", "xargs", "tee", "sort", "uniq", "wc", "date",
    "printf", "echo", "test", "true", "false", "dirname", "basename",
}
# 孤儿豁免：launchd/systemd 直管的常驻守护进程本来 ppid 就是 1
DEFAULT_ORPHAN_WHITELIST = {
    "launchd", "init", "systemd", "dbus-daemon", "proclens",
}

# ---------------------------------------------------------------- etime 解析

def parse_etime(text):
    """ps 的 etime 形态: [([dd-]hh:]mm:]ss。返回秒数，解析不了返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip()
    days = 0
    if "-" in t:
        d, _, rest = t.partition("-")
        if not d.isdigit():
            return None
        days = int(d)
        t = rest
    parts = t.split(":")
    if not all(p.isdigit() for p in parts):
        return None
    if len(parts) == 1:
        return days * 86400 + int(parts[0])
    if len(parts) == 2:
        return days * 86400 + int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return None


def fmt_etime(seconds):
    if seconds is None:
        return "?"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dd%02d:%02d:%02d" % (d, h, m, s)
    return "%02d:%02d:%02d" % (h, m, s)

# ---------------------------------------------------------------- 脱敏

_TOKEN_PATTERNS = [
    re.compile(r"gh[posuhr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<!\w)[A-Fa-f0-9]{32}(?!\w)"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(token|password|passwd|secret)[= ]\S+"),
]


def mask_secrets(text):
    """命令行里像凭据的片段一律前4后4，中间打码。"""
    if not text:
        return text
    out = text

    def _mask(m):
        v = m.group(0)
        if len(v) <= 10:
            return v[:2] + "…" + v[-2:]
        return v[:4] + "…" + v[-4:]

    for pat in _TOKEN_PATTERNS:
        out = pat.sub(_mask, out)
    return out

# ---------------------------------------------------------------- 进程表解析

def proc_name(cmd):
    if not cmd:
        return "?"
    first = cmd.split()[0] if cmd.split() else "?"
    return os.path.basename(first)


def parse_ps_text(text):
    """把 ps 输出解析成进程行 dict 列表；坏行跳过不炸。"""
    rows = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid_s, ppid_s, user, etime_s, state, pcpu_s, rss_s, cmd = parts
        if not pid_s.isdigit() or not ppid_s.isdigit():
            continue
        etime = parse_etime(etime_s)
        if etime is None:
            continue
        try:
            pcpu = float(pcpu_s)
            rss_kb = float(rss_s)
        except ValueError:
            continue
        st = (state or "?")[0]
        rows.append({
            "pid": int(pid_s), "ppid": int(ppid_s), "user": user,
            "etime_s": etime, "etime_raw": etime_s, "state": st,
            "pcpu": pcpu, "rss_kb": rss_kb, "cmd": cmd, "name": proc_name(cmd),
        })
    return rows


def run_ps(_args=None):
    r = subprocess.run(PS_CMD, capture_output=True, text=True, timeout=30)
    return r.stdout

# ---------------------------------------------------------------- 发现器

FINDING_ORDER = {"crit": 0, "warn": 1, "info": 2}


def _mk(kind, severity, row, detail):
    return {
        "kind": kind, "severity": severity,
        "pid": row.get("pid"), "ppid": row.get("ppid"),
        "user": row.get("user", "?"), "name": row.get("name", "?"),
        "etime_s": row.get("etime_s"), "etime_raw": row.get("etime_raw", "?"),
        "cmd": mask_secrets(row.get("cmd", ""))[:160], "detail": detail,
    }


def find_zombies(rows):
    """僵尸：state=Z，父进程没 wait。"""
    out = []
    for r in rows:
        if r["state"] == "Z":
            out.append(_mk("ZOMBIE", "warn", r, "子进程已死父进程未收尸（父 pid %d）" % r["ppid"]))
    return out


def is_self(row):
    """proclens 自己的行：cmd 里带 proclens（透过 python3/env 启动也认得）。"""
    return "proclens" in (row.get("cmd") or "")


def filter_self(rows):
    """把 proclens 自己的行剔出进程表：基线与 diff 不该看见自己。"""
    return [r for r in rows if not is_self(r)]


def find_orphans(rows, fresh_s, whitelist):
    """新晋孤儿：ppid 是 1 且进程很年轻 —— 父进程先走了一步。
    ppid=1 的常驻守护进程（etime 很长）是正常形态，不报。
    whitelist 只做「追加」：默认豁免永远生效。"""
    wl = DEFAULT_ORPHAN_WHITELIST | set(w for w in (whitelist or ()) if w)
    out = []
    for r in rows:
        if is_self(r):
            continue
        if r["ppid"] in (0, 1) and r["etime_s"] <= fresh_s and r["name"] not in wl:
            out.append(_mk("ORPHAN", "info", r, "父进程已退场，进程 %ds 前被托管给 pid 1" % r["etime_s"]))
    return out


def find_loop_suspects(rows, min_pids, fresh_s, whitelist):
    """快照级 crash-loop 嫌疑：同名 >=min_pids 个实例且全部年轻。
    只给 info（单帧证据不足，反复横死的实锤在 watch 的轮次里）。"""
    groups = {}
    wl = DEFAULT_LOOP_WHITELIST | set(w for w in (whitelist or ()) if w)
    for r in rows:
        if r["name"] in wl or is_self(r):
            continue
        groups.setdefault(r["name"], []).append(r)
    out = []
    for name, grp in groups.items():
        if len(grp) >= min_pids and all(g["etime_s"] <= fresh_s for g in grp):
            pids = ",".join(str(g["pid"]) for g in grp[:6])
            out.append(_mk("LOOP_SUSPECT", "info", grp[0],
                           "同名 %d 个实例全部诞生于 %ds 内（pid %s）——疑似批量重生"
                           % (len(grp), fresh_s, pids)))
    return out


class Watcher:
    """多轮采样：pid 集合追踪 + 名字生死双轨判据。

    - 名字整体消失又回来（respawn）→ 服务整个死过 → CRIT
    - 名字常驻但 pid 不断换血（池化 worker 重生，名字永远在场）→
      按「见过的不同 pid 数」梯度判级：>=pid_churn_threshold WARN，
      >=2 倍阈值 CRIT —— 换血速度本身就是 crash loop / fork 风暴的实锤
    """

    def __init__(self, respawn_threshold=2, pid_churn_threshold=4, whitelist=None):
        self.respawn_threshold = respawn_threshold
        self.pid_churn_threshold = pid_churn_threshold
        self.whitelist = DEFAULT_LOOP_WHITELIST | set(w for w in (whitelist or ()) if w)
        self.rounds = []
        self.sight = {}   # name -> {"alive_rounds","deaths","respawns","last","pids":set}

    def feed(self, rows, round_no):
        alive = {}
        for r in rows:
            if r["state"] != "Z":
                alive.setdefault(r["name"], set()).add(r["pid"])
        self.rounds.append(round_no)
        for name in set(list(self.sight.keys()) + list(alive)):
            if name in self.whitelist:
                continue
            rec = self.sight.setdefault(name, {"alive_rounds": 0, "deaths": 0,
                                               "respawns": 0, "last": False, "pids": set()})
            now = name in alive
            rec["pids"] |= alive.get(name, set())
            if now:
                rec["alive_rounds"] += 1
                if rec["last"] is False and rec["alive_rounds"] > 1:
                    rec["respawns"] += 1
            else:
                if rec["last"] is True:
                    rec["deaths"] += 1
            rec["last"] = now

    def findings(self):
        out = []
        rounds = len(self.rounds)
        for name, rec in self.sight.items():
            distinct = len(rec["pids"])
            if rec["respawns"] >= self.respawn_threshold:
                out.append({
                    "kind": "CHURN", "severity": "crit",
                    "pid": None, "ppid": None, "user": "?", "name": name,
                    "etime_s": None, "etime_raw": "-",
                    "cmd": name, "detail": "%d 轮内整体死亡 %d 次、重生 %d 次 —— 服务整个死过又爬起来，crash loop 实锤"
                                           % (rounds, rec["deaths"], rec["respawns"]),
                })
            elif rounds >= 2 and rec["alive_rounds"] >= 2 and distinct >= self.pid_churn_threshold:
                sev = "crit" if distinct >= 2 * self.pid_churn_threshold else "warn"
                out.append({
                    "kind": "CHURN_PID", "severity": sev,
                    "pid": None, "ppid": None, "user": "?", "name": name,
                    "etime_s": None, "etime_raw": "-",
                    "cmd": name, "detail": "%d 轮内同名进程换血 %d 个不同 pid（名字常驻、pid 不断重生）——池化 worker 疑似反复横死"
                                           % (rounds, distinct),
                })
        sight_out = {k: {**v, "pids": sorted(v["pids"])} for k, v in self.sight.items()}
        return out, sight_out

# ---------------------------------------------------------------- diff / baseline

def _sig(row):
    return row["name"] + "|" + row["cmd"]


def diff_rows(base_rows, now_rows, restart_fresh_s, whitelist=None):
    """基线比对。瞬时工具（ps/sleep/bash 等豁免名单成员）不参与：
    秒级窗口里它们的新陈代谢全是噪音，基线只关心服务。"""
    wl = DEFAULT_LOOP_WHITELIST if whitelist is None else set(whitelist)
    base = {r["pid"]: r for r in base_rows if r["name"] not in wl}
    now = {r["pid"]: r for r in now_rows if r["name"] not in wl}
    out = []
    for pid, r in base.items():
        if pid not in now:
            out.append({"event": "DEAD", "severity": "info", "pid": pid,
                        "name": r["name"], "cmd": mask_secrets(r["cmd"])[:120],
                        "detail": "基线 pid %d 已退场（%s）" % (pid, r["name"])})
    for pid, r in now.items():
        if pid not in base:
            out.append({"event": "NEW", "severity": "info", "pid": pid,
                        "name": r["name"], "cmd": mask_secrets(r["cmd"])[:120],
                        "detail": "新面孔 pid %d（etime %s）" % (pid, fmt_etime(r["etime_s"]))})
    # 重启：同签名换了 pid，且新 pid 很年轻
    base_sigs = {}
    for pid, r in base.items():
        base_sigs.setdefault(_sig(r), set()).add(pid)
    seen_pairs = set()
    for pid, r in now.items():
        sig = _sig(r)
        if pid not in base and sig in base_sigs and pid not in base_sigs[sig]:
            old = min(base_sigs[sig])
            pair = (old, pid)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if r["etime_s"] <= restart_fresh_s:
                out.append({"event": "RESTARTED", "severity": "warn", "pid": pid,
                            "name": r["name"], "cmd": mask_secrets(r["cmd"])[:120],
                            "detail": "同进程换 pid 重启：旧 %d → 新 %d（etime %s）"
                                      % (old, pid, fmt_etime(r["etime_s"]))})
    # etime 倒流 = 数据异常
    for pid, r in now.items():
        if pid in base and r["etime_s"] < base[pid]["etime_s"]:
            out.append({"event": "ETIME_RESET", "severity": "crit", "pid": pid,
                        "name": r["name"], "cmd": mask_secrets(r["cmd"])[:120],
                        "detail": "pid %d 的 etime 倒流（%d → %d 秒）：计数异常或 pid 复用"
                                  % (pid, base[pid]["etime_s"], r["etime_s"])})
    return out

# ---------------------------------------------------------------- 汇总

def assemble(rows, args_like):
    """一条进程只产一条主发现，优先级 ZOMBIE > LOOP > ORPHAN（去重免噪音）。"""
    wl_loop = set(DEFAULT_LOOP_WHITELIST) | set(args_like.get("loop_whitelist", []))
    wl_orph = set(DEFAULT_ORPHAN_WHITELIST) | set(args_like.get("orphan_whitelist", []))
    claimed = set()
    findings = []
    zombies = find_zombies(rows)
    findings += zombies
    claimed.update(z["pid"] for z in zombies)
    loops = find_loop_suspects(rows, args_like.get("loop_min_pids", 3),
                               args_like.get("loop_fresh_s", 120), wl_loop)
    findings += loops
    for lf in loops:
        for r in rows:
            if r["name"] == lf["name"]:
                claimed.add(r["pid"])
    for f in find_orphans(rows, args_like.get("orphan_fresh_s", 300), wl_orph):
        if f["pid"] not in claimed:
            findings.append(f)
    return findings


def summarize(findings):
    c = sum(1 for f in findings if f["severity"] == "crit")
    w = sum(1 for f in findings if f["severity"] == "warn")
    i = sum(1 for f in findings if f["severity"] == "info")
    return {"crit": c, "warn": w, "info": i, "total": len(findings)}


def exit_code_for(summary):
    if summary["crit"] > 0:
        return EXIT_CRIT
    if summary["warn"] > 0:
        return EXIT_WARN
    return EXIT_OK

# ---------------------------------------------------------------- 输出

SEV_TAG = {"crit": "[CRIT]", "warn": "[WARN]", "info": "[INFO]"}


def print_text(scanned, findings, extra=""):
    s = summarize(findings)
    print("proclens v%s — 进程生命周期透镜" % VERSION)
    print("进程表: %d 进程已扫描 | 发现 %d 条 (CRIT %d / WARN %d / INFO %d)"
          % (scanned, s["total"], s["crit"], s["warn"], s["info"]))
    if extra:
        print(extra)
    for f in sorted(findings, key=lambda x: FINDING_ORDER.get(x["severity"], 9)):
        pid = f.get("pid")
        pid_s = ("pid %s" % pid) if pid is not None else "name %s" % f["name"]
        print("%s %-12s %-16s %s etime %s" % (SEV_TAG[f["severity"]], f["kind"],
                                              f["name"], pid_s, f["etime_raw"]))
        print("       %s" % f["detail"])
        if f.get("cmd") and f["cmd"] != f["name"]:
            print("       cmd: %s" % f["cmd"])
    return s


def print_diff(events):
    print("proclens v%s — 基线比对" % VERSION)
    counts = {}
    for e in events:
        counts[e["event"]] = counts.get(e["event"], 0) + 1
    print("事件: %s" % (" / ".join("%s %d" % (k, v) for k, v in sorted(counts.items())) or "无变化"))
    for e in events:
        print("%-9s %-8s %s" % (e["event"], SEV_TAG[e["severity"]], e["detail"]))
    sev = max((e["severity"] for e in events), key=lambda s: -FINDING_ORDER.get(s, 9), default=None)
    if sev == "crit":
        return EXIT_CRIT
    if sev == "warn":
        return EXIT_WARN
    return EXIT_OK

# ---------------------------------------------------------------- selftest

def _selftest():
    import tempfile
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    # 1 etime 全形态
    check("etime_ss", parse_etime("37") == 37)
    check("etime_mmss", parse_etime("12:05") == 725)
    check("epoch_hhmmss", parse_etime("03:12:05") == 3 * 3600 + 12 * 60 + 5)
    check("etime_dd_hhmmss", parse_etime("02-03:12:05") == 2 * 86400 + 3 * 3600 + 12 * 60 + 5)
    check("etime_invalid_none", parse_etime("xx:yy") is None)
    check("etime_empty_none", parse_etime("") is None)

    # 2 ps 行解析
    good = " 1234   567 alice   01:02:03 S   0.5  64000 /usr/bin/python3 -m jobs --flag"
    rows = parse_ps_text(good)
    check("ps_line_fields", len(rows) == 1 and rows[0]["pid"] == 1234
          and rows[0]["ppid"] == 567 and rows[0]["name"] == "python3"
          and rows[0]["etime_s"] == 3723)
    multi = good + "\n   99     1 bob   05:00 Z  99.0     32 /bin/deadchild\n" \
                  "bad line not enough\n  abc   1 x 1:1 S 1 1 /badpid\n" \
                  "  77     1 bob  bad!time S 1 1 /badetime\n"
    rows2 = parse_ps_text(multi)
    check("ps_multi_and_skip_bad", len(rows2) == 2 and rows2[1]["state"] == "Z")

    # 3 脱敏（fixture 全部运行时拼接构造——token 形状的完整字面量不落盘，也过不了防泄漏层）
    ghp = "ghp_" + "a1b2c3d4e5f6a7b8"
    check("mask_ghp", "…" in mask_secrets("token " + ghp) and ghp not in mask_secrets("cmd " + ghp))
    sk = "sk-" + "ABCDEF0123456789abcdef01"
    check("mask_sk", sk not in mask_secrets("openai " + sk) and "…" in mask_secrets("openai " + sk))
    hx = "a1b2c3d4e5f6a7b8" + "4d5e6f708192a3b4"
    check("mask_hex32", hx not in mask_secrets("curl -H " + hx))
    br = "Bearer " + "q1w2e3r4t5y6u7i8o9p0"
    check("mask_bearer", "q1w2e3r4t5y6u7i8o9p0" not in mask_secrets("Authorization: " + br))
    kv = "password" + "=zz0y9x8w7v6u5t4s3r2"
    check("mask_kv", "zz0y9x8w7v6u5t4s3r2" not in mask_secrets("myapp --" + kv))
    check("mask_plain_untouched", mask_secrets("/usr/bin/python3 -m jobs --flag x") == "/usr/bin/python3 -m jobs --flag x")

    # 4 僵尸
    ztxt = "   99   999 bob   05:00 Z  99.0     32 /bin/deadchild\n" \
           "  100   999 bob   1:00:00 S   0.0   128 /bin/parent\n"
    zr = parse_ps_text(ztxt)
    zf = find_zombies(zr)
    check("zombie_found", len(zf) == 1 and zf[0]["severity"] == "warn" and zf[0]["pid"] == 99)
    check("zombie_parent_in_detail", "999" in zf[0]["detail"])
    ztxt2 = "   99     1 bob   05:00 Z  99.0     32 /bin/deadchild\n"
    check("zombie_orphaned_parent_still_listed", len(find_zombies(parse_ps_text(ztxt2))) == 1)

    # 5 孤儿
    otxt = "  200     1 bob   00:00:45 S   0.0   128 /usr/local/bin/freshdaemon\n" \
           "  201     1 bob 12-03:00:00 S   0.0   128 /usr/sbin/oldboot\n" \
           "  202     1 bob   00:00:10 S   0.0   128 /usr/sbin/launchd\n"
    orows = parse_ps_text(otxt)
    of = find_orphans(orows, 300, DEFAULT_ORPHAN_WHITELIST)
    check("orphan_fresh_flagged", len(of) == 1 and of[0]["pid"] == 200 and of[0]["severity"] == "info")
    check("orphan_longlived_silent", all(f["pid"] != 201 for f in of))
    check("orphan_whitelist_silent", all(f["pid"] != 202 for f in of))
    of2 = find_orphans(orows, 300, {"freshdaemon"})
    check("orphan_custom_whitelist_append", len(of2) == 0)  # 200 被追加豁免；201 太老；202 launchd 默认豁免

    # 6 快照 loop 嫌疑
    lrows = parse_ps_text(
        "  300   1 u 00:00:05 S 1 32 /opt/app/worker\n"
        "  301   1 u 00:00:07 S 1 32 /opt/app/worker\n"
        "  302   1 u 00:00:09 S 1 32 /opt/app/worker\n")
    lf = find_loop_suspects(lrows, 3, 120, DEFAULT_LOOP_WHITELIST)
    check("loop_suspect_grouped", len(lf) == 1 and lf[0]["name"] == "worker")
    lrows2 = parse_ps_text(
        "  400   1 u 00:00:05 S 1 32 /opt/app/worker\n"
        "  401   1 u 03:00:00 S 1 32 /opt/app/worker\n"
        "  402   1 u 05:00:00 S 1 32 /opt/app/worker\n")
    check("loop_mixed_age_not_flagged", len(find_loop_suspects(lrows2, 3, 120, DEFAULT_LOOP_WHITELIST)) == 0)
    lrows3 = parse_ps_text(
        "  500   1 u 00:00:05 S 1 32 /opt/app/mdworker\n"
        "  501   1 u 00:00:06 S 1 32 /opt/app/mdworker\n"
        "  502   1 u 00:00:07 S 1 32 /opt/app/mdworker\n")
    check("loop_whitelist_silent", len(find_loop_suspects(lrows3, 3, 120, DEFAULT_LOOP_WHITELIST)) == 0)
    check("loop_below_min_pids_silent", len(find_loop_suspects(lrows[:2], 3, 120, DEFAULT_LOOP_WHITELIST)) == 0)

    # 7 watch 轮次（fixture：worker 三轮 生/死/生）
    w = Watcher(respawn_threshold=2)
    r1 = parse_ps_text("  600 1 u 00:00:30 S 1 32 /opt/app/worker\n  700 1 u 10:00:00 S 1 32 /sbin/init-ish\n")
    r2 = parse_ps_text("  700 1 u 10:01:00 S 1 32 /sbin/init-ish\n")
    r3 = parse_ps_text("  601 1 u 00:00:02 S 1 32 /opt/app/worker\n  700 1 u 10:02:00 S 1 32 /sbin/init-ish\n")
    r4 = parse_ps_text("  700 1 u 10:03:00 S 1 32 /sbin/init-ish\n")
    r5 = parse_ps_text("  602 1 u 00:00:01 S 1 32 /opt/app/worker\n  700 1 u 10:04:00 S 1 32 /sbin/init-ish\n")
    for i, rr in enumerate([r1, r2, r3, r4, r5], 1):
        w.feed(rr, i)
    wf, sight = w.findings()
    check("watch_respawn_counted", len(wf) == 1 and wf[0]["severity"] == "crit"
          and wf[0]["kind"] == "CHURN" and "重生 2 次" in wf[0]["detail"])
    check("watch_stable_silent", sight.get("init-ish", {}).get("respawns", 0) == 0)
    w2 = Watcher(respawn_threshold=2)
    for i, rr in enumerate([r1, r2, r1, r2], 1):   # 只死 1 次
        rr = parse_ps_text(rr and ("  600 1 u 00:00:30 S 1 32 /opt/app/worker\n") if i % 2 else
                           "  700 1 u 10:00:00 S 1 32 /sbin/init-ish\n")
        w2.feed(rr, i)
    wf2, _ = w2.findings()
    check("watch_below_threshold_silent", len(wf2) == 0)

    # 7b 池化换血：名字常驻、pid 每轮换新（旧判据完全看不见的形态）
    pool_rows = ["  %d 1 u 00:00:0%d S 1 32 /opt/app/poolworker\n" % (700 + i, i) for i in range(5)]
    w3 = Watcher(respawn_threshold=2, pid_churn_threshold=4)
    for i in range(5):
        w3.feed(parse_ps_text(pool_rows[i]), i + 1)
    wf3, sight3 = w3.findings()
    poolf = [f for f in wf3 if f["name"] == "poolworker"]
    check("churn_pid_pooling_detected", len(poolf) == 1 and poolf[0]["kind"] == "CHURN_PID"
          and poolf[0]["severity"] == "warn" and "5 个不同 pid" in poolf[0]["detail"])
    check("churn_pid_respawn_not_double_counted", sight3["poolworker"]["respawns"] == 0)
    # 换血速度翻倍 → CRIT
    w4 = Watcher(respawn_threshold=2, pid_churn_threshold=4)
    for i in range(9):
        w4.feed(parse_ps_text("  %d 1 u 00:00:01 S 1 32 /opt/app/poolworker\n" % (800 + i)), i + 1)
    wf4, _ = w4.findings()
    poolf4 = [f for f in wf4 if f["name"] == "poolworker"]
    check("churn_pid_graded_crit", len(poolf4) == 1 and poolf4[0]["severity"] == "crit")
    # 同一 pid 常驻 → 静默
    w5 = Watcher(respawn_threshold=2, pid_churn_threshold=4)
    for i in range(5):
        w5.feed(parse_ps_text("  900 1 u 10:00:00 S 1 32 /opt/app/stable\n"), i + 1)
    check("churn_pid_stable_silent", len(w5.findings()[0]) == 0)
    # shell 工具箱豁免：sleep 换血再多也不报
    w6 = Watcher(respawn_threshold=2, pid_churn_threshold=2)
    for i in range(6):
        w6.feed(parse_ps_text("  %d 1 u 00:00:01 S 1 32 /bin/sleep 1\n" % (950 + i)), i + 1)
    check("churn_whitelist_shell_tools_silent", len(w6.findings()[0]) == 0)
    # 真实服务整体死两次 → 仍走 CHURN crit
    w7 = Watcher(respawn_threshold=2, pid_churn_threshold=4)
    seq = [parse_ps_text("  960 1 u 00:00:30 S 1 32 /opt/app/api\n"),
           parse_ps_text(""),
           parse_ps_text("  961 1 u 00:00:02 S 1 32 /opt/app/api\n"),
           parse_ps_text(""),
           parse_ps_text("  962 1 u 00:00:02 S 1 32 /opt/app/api\n")]
    for i, rr in enumerate(seq, 1):
        w7.feed(rr, i)
    wf7, _ = w7.findings()
    apif = [f for f in wf7 if f["name"] == "api"]
    check("churn_respawn_still_crit", len(apif) == 1 and apif[0]["kind"] == "CHURN"
          and apif[0]["severity"] == "crit" and "重生 2 次" in apif[0]["detail"])

    # 8 diff / baseline
    base = parse_ps_text(
        "  800   1 u 10:00:00 S 1 32 /opt/app/server\n"
        "  801   1 u 10:00:00 S 1 32 /opt/app/dying\n")
    now = parse_ps_text(
        "  900   1 u 00:00:03 S 1 32 /opt/app/server\n"
        "  901   1 u 02:00:00 S 1 32 /opt/app/newcomer\n")
    ev = diff_rows(base, now, 300)
    kinds = sorted(e["event"] for e in ev)
    check("diff_dead_new", "DEAD" in kinds and "NEW" in kinds)
    check("diff_restarted", any(e["event"] == "RESTARTED" and e["pid"] == 900 for e in ev))
    ev2 = diff_rows(base, parse_ps_text(
        "  800   1 u 09:00:00 S 1 32 /opt/app/server\n"), 300)
    check("diff_etime_reset_crit", any(e["event"] == "ETIME_RESET" for e in ev2))
    # baseline 往返
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    slim = [{k: r[k] for k in ("pid", "ppid", "user", "name", "etime_s", "etime_raw",
                               "state", "pcpu", "rss_kb", "cmd")} for r in base]
    with open(tmp.name, "w", encoding="utf-8") as fh:
        json.dump(slim, fh)
    with open(tmp.name, encoding="utf-8") as fh:
        loaded = json.load(fh)
    check("baseline_roundtrip", diff_rows(loaded, base, 300) == [])
    check("baseline_slim_rows_diffable", all("name" in r for r in loaded))
    # 瞬时工具不进 diff：ps/sleep 新陈代谢不产事件
    tr_base = parse_ps_text("  970   1 u 10:00:00 S 1 32 /opt/app/api\n"
                            "  971   1 u 00:00:00 S 1 32 ps -eo\n")
    tr_now = parse_ps_text("  972   1 u 00:00:01 S 1 32 /opt/app/api\n"
                           "  973   1 u 00:00:00 S 1 32 sleep 60\n")
    tr_ev = diff_rows(tr_base, tr_now, 300)
    check("diff_transient_tools_silent", all(e["name"] not in ("ps", "sleep") for e in tr_ev)
          and any(e["event"] == "RESTARTED" and e["name"] == "api" for e in tr_ev))
    os.unlink(tmp.name)

    # 9 汇总与退出码
    s = summarize([{"severity": "crit"}, {"severity": "warn"}, {"severity": "warn"}, {"severity": "info"}])
    check("summary_counts", s == {"crit": 1, "warn": 2, "info": 1, "total": 4})
    check("exit_clean0", exit_code_for({"crit": 0, "warn": 0, "info": 3}) == EXIT_OK)
    check("exit_warn2", exit_code_for({"crit": 0, "warn": 1, "info": 3}) == EXIT_WARN)
    check("exit_crit3", exit_code_for({"crit": 1, "warn": 0, "info": 0}) == EXIT_CRIT)

    # 10 JSON schema 稳定
    f0 = assemble(parse_ps_text(otxt), {})
    keys = set(f0[0].keys())
    need = {"kind", "severity", "pid", "ppid", "user", "name", "etime_s", "etime_raw", "cmd", "detail"}
    check("json_schema_fields", need.issubset(keys))

    # 11 空输入不炸
    check("empty_ps_ok", parse_ps_text("") == [] and find_zombies([]) == []
          and assemble(parse_ps_text(""), {}) == [])

    # 12 自排除：proclens 自己的行不进孤儿名单，也不进基线
    selfrows = parse_ps_text("  4242     1 u 00:00:02 S 1 32 /usr/bin/env python3 proclens.py scan\n")
    check("self_excluded", all(f["pid"] != 4242 for f in
                               find_orphans(selfrows, 300, DEFAULT_ORPHAN_WHITELIST)))
    mixed = parse_ps_text("  4242     1 u 00:00:02 S 1 32 /usr/bin/env python3 proclens.py scan\n"
                          "  4243   424 u 01:00:00 S 1 32 /opt/app/server\n")
    kept = filter_self(mixed)
    check("filter_self_keeps_others", [r["pid"] for r in kept] == [4243])

    # 13 演示森林确定性（fixture 复现完整发现链）
    demo = parse_ps_text(
        "  910     1 u 00:00:04 Z  0.0  16 /bin/demo_zombie\n"
        "  911     1 u 00:00:20 S  0.0  32 /bin/demo_orphan\n"
        "  920     1 u 00:00:06 S  0.0  32 /opt/demo/looper\n"
        "  921     1 u 00:00:08 S  0.0  32 /opt/demo/looper\n"
        "  922     1 u 00:00:11 S  0.0  32 /opt/demo/looper\n")
    df = assemble(demo, {})
    dkinds = sorted(f["kind"] for f in df)
    check("demo_forest_chain", dkinds == ["LOOP_SUSPECT", "ORPHAN", "ZOMBIE"])

    ok = sum(1 for _, c in results if c)
    for name, c in results:
        print("%s %s" % ("PASS" if c else "FAIL", name))
    print("selftest: %d/%d GREEN" % (ok, len(results)))
    return 0 if ok == len(results) else 4

# ---------------------------------------------------------------- CLI

def main(argv):
    ap = argparse.ArgumentParser(prog="proclens", description="进程生命周期透镜：僵尸 / 新晋孤儿 / 反复横死")
    ap.add_argument("--version", action="version", version="proclens %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="扫当前进程表（僵尸/孤儿/loop 嫌疑）")
    p_scan.add_argument("--json", action="store_true", dest="as_json")
    p_scan.add_argument("--fixture", help="用文件里的 ps 输出代替真实 ps（测试/复现）")
    p_scan.add_argument("--orphan-fresh-s", type=int, default=300)
    p_scan.add_argument("--loop-min-pids", type=int, default=3)
    p_scan.add_argument("--loop-fresh-s", type=int, default=120)
    p_scan.add_argument("--loop-whitelist", default="", help="逗号分隔，追加 loop 豁免名")
    p_scan.add_argument("--orphan-whitelist", default="", help="逗号分隔，追加孤儿豁免名")
    p_scan.add_argument("--save-baseline", metavar="FILE", help="把本次扫描存为基线")
    p_scan.add_argument("--baseline", metavar="FILE", help="与基线比对（NEW/DEAD/RESTARTED）")
    p_scan.add_argument("--restart-fresh-s", type=int, default=300)

    p_watch = sub.add_parser("watch", help="多轮采样抓反复横死（crash loop 实锤）")
    p_watch.add_argument("--interval", type=float, default=5.0)
    p_watch.add_argument("--rounds", type=int, default=6)
    p_watch.add_argument("--respawn-threshold", type=int, default=2)
    p_watch.add_argument("--pid-churn-threshold", type=int, default=4,
                         help="单轮次内见过的不同 pid 数达到该值即判池化换血 WARN，翻倍判 CRIT")
    p_watch.add_argument("--loop-whitelist", default="", help="逗号分隔，追加 loop/churn 豁免名")
    p_watch.add_argument("--json", action="store_true", dest="as_json")
    p_watch.add_argument("--fixture", help="逗号分隔的多轮 fixture 文件（测试/复现）")

    sub.add_parser("selftest", help="跑不变量自测")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return EXIT_USAGE
    if args.cmd == "selftest":
        return _selftest()

    like = {
        "loop_whitelist": [x for x in getattr(args, "loop_whitelist", "").split(",") if x],
        "orphan_whitelist": [x for x in getattr(args, "orphan_whitelist", "").split(",") if x],
        "orphan_fresh_s": getattr(args, "orphan_fresh_s", 300),
        "loop_min_pids": getattr(args, "loop_min_pids", 3),
        "loop_fresh_s": getattr(args, "loop_fresh_s", 120),
    }

    def get_ps_text(fix):
        if fix:
            with open(fix, encoding="utf-8") as fh:
                return fh.read()
        return run_ps()

    if args.cmd == "watch":
        rounds_src = args.fixture.split(",") if args.fixture else None
        w = Watcher(respawn_threshold=args.respawn_threshold,
                    pid_churn_threshold=args.pid_churn_threshold,
                    whitelist=like["loop_whitelist"])
        for i in range(1, args.rounds + 1):
            text = get_ps_text(rounds_src[i - 1]) if rounds_src else run_ps()
            w.feed(parse_ps_text(text), i)
            alive = sum(1 for s in w.sight.values() if s["last"])
            print("round %d/%d: %d 个进程名在场" % (i, args.rounds, alive))
            if not rounds_src and i < args.rounds:
                time.sleep(args.interval)
        findings, sight = w.findings()
        scanned = sum(r["alive_rounds"] for r in sight.values())
        if args.as_json:
            print(json.dumps({"version": VERSION, "mode": "watch", "rounds": args.rounds,
                              "findings": findings, "sight": sight,
                              "summary": summarize(findings)}, ensure_ascii=False, indent=2))
            return exit_code_for(summarize(findings))
        print_text(scanned, findings,
                   extra="watch: %d 轮采样, 重生阈值 %d" % (args.rounds, args.respawn_threshold))
        return exit_code_for(summarize(findings))

    # scan
    text = get_ps_text(args.fixture)
    rows = filter_self(parse_ps_text(text))
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as fh:
            base_rows = json.load(fh)
        events = diff_rows(base_rows, rows, args.restart_fresh_s)
        if args.as_json:
            print(json.dumps({"version": VERSION, "mode": "diff", "events": events,
                              "summary": {"total": len(events)}}, ensure_ascii=False, indent=2))
            return EXIT_OK
        return print_diff(events)
    findings = assemble(rows, like)
    if args.save_baseline:
        with open(args.save_baseline, "w", encoding="utf-8") as fh:
            json.dump([{k: r[k] for k in ("pid", "ppid", "user", "name", "etime_s",
                                          "etime_raw", "state", "pcpu", "rss_kb", "cmd")}
                       for r in rows], fh, ensure_ascii=False)
    if args.as_json:
        s = summarize(findings)
        print(json.dumps({"version": VERSION, "mode": "scan", "scanned": len(rows),
                          "findings": findings, "summary": s}, ensure_ascii=False, indent=2))
        return exit_code_for(s)
    s = print_text(len(rows), findings)
    return exit_code_for(s)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # 内部错误带信息退出
        sys.stderr.write("proclens internal error: %r\n" % (exc,))
        sys.exit(4)
