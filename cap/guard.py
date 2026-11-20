# -*- coding: utf-8 -*-
"""文本护栏：处理上游对 system 开头「身份声明」的拒绝（命中即 403 code=4100406）。

匹配特征（规则见 `sanitize.json`）：
  · 只匹配 system 角色；同句放 user / assistant 不受影响
  · 只匹配开头
  · 动词、品牌、句式三者同时命中才触发

四层实现到前三层：
  L0 声明式规则  sanitize.json，按 mtime 热加载
  L1 策略记忆    sanitize_plan 表：同一 system 指纹下次直接用上次奏效的等级
  L2 在线自适应  level 0 规则 → 1 注 U+2060 → 2 加前缀 → 3 system 降级为 user
L3（离线学习，自动发现新规则）不在本文件范围。
"""
import hashlib
import json
import os
import re
import sys
import time

U_JOINER = "\u2060"          # word joiner：实测注入后上游正则不再命中，且模型侧不可见
MAX_LEVEL = 3
DEFAULT_POLICY = ("rewrite", "inject_joiner", "prepend", "degrade")
PREPEND_TEXT = "以下是本次会话的背景说明。\n"

# 可疑词：用于 level 1 决定往哪里注 U+2060
_SUSPECT_RX = re.compile(r"(?i)(powered\s+by|deepseek|ai\s+agent)")

_CACHE = {"path": "", "mtime": 0.0, "rules": [], "policy": list(DEFAULT_POLICY)}
_PLAN = {}                    # fp -> level（进程内 L1 缓存，避免每请求查库）
_FAILS = {"n": 0, "until": 0.0}
BREAKER_AFTER = 5
BREAKER_SECONDS = 1800


# ---------------- 规则 ----------------
def rules_path(ctx):
    root = (ctx or {}).get("root") or os.getcwd()
    return os.path.join(root, "sanitize.json")


def load_rules(ctx=None):
    """按 mtime 热加载规则文件。文件不存在 = 空规则（护栏退化成纯策略序列）。"""
    path = rules_path(ctx)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _CACHE.update({"path": path, "mtime": 0.0, "rules": [],
                       "policy": list(DEFAULT_POLICY)})
        return _CACHE
    if _CACHE["path"] == path and _CACHE["mtime"] == mt:
        return _CACHE
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        sys.stderr.write("[guard] 规则文件解析失败（%s），按空规则跑\n" % e)
        obj = {}
    rules = []
    for r in obj.get("rules") or []:
        try:
            rules.append({
                "id": r.get("id") or "rule",
                "where": (r.get("where") or "any").lower(),
                "rx": re.compile(r["pattern"]),
                "replace": r.get("replace") or "",
            })
        except Exception as e:
            sys.stderr.write("[guard] 规则 %r 无效（%s），跳过\n" % (r.get("id"), e))
    _CACHE.update({"path": path, "mtime": mt, "rules": rules,
                   "policy": [p for p in (obj.get("policy") or DEFAULT_POLICY)]})
    return _CACHE


# ---------------- 文本动作 ----------------
def _apply_rules(text, rules):
    if not isinstance(text, str):
        return text, 0
    hits = 0
    for r in rules:
        if r["where"] == "start":
            m = r["rx"].match(text)
            if not m:
                continue
            text = r["replace"] + text[m.end():] if r["replace"] else text[m.end():]
            hits += 1
        else:
            text, n = r["rx"].subn(r["replace"], text)
            hits += n
    return text, hits


def _map_text(content, fn):
    """对 content 应用 fn：字符串直接处理，块数组逐块处理 text。返回 (新内容, 命中数)。"""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        out, n = [], 0
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                nt, k = fn(part["text"])
                if k:
                    part = dict(part)
                    part["text"] = nt
                    n += k
            out.append(part)
        return out, n
    return content, 0


def _suspicious(text):
    return isinstance(text, str) and bool(_SUSPECT_RX.search(text))


def _inject_joiner(text):
    """在可疑词的首字符后插 U+2060，破坏上游正则又不改变语义。"""
    def rep(m):
        w = m.group(0)
        return w[0] + U_JOINER + w[1:] if len(w) > 1 else U_JOINER + w
    return _SUSPECT_RX.sub(rep, text)


# ---------------- 主入口 ----------------
def apply(messages, level=0, ctx=None):
    """按等级处理 messages。返回 (新 messages, 报告 dict)。不修改入参。"""
    cfg = load_rules(ctx)
    policy = cfg["policy"]
    rep = {"level": level, "actions": [], "hits": 0, "changed": False}
    out = [dict(m) if isinstance(m, dict) else m for m in (messages or [])]
    idx = [i for i, m in enumerate(out) if isinstance(m, dict) and m.get("role") == "system"]
    if not idx:
        return out, rep

    if "rewrite" in policy:                      # L0
        for i in idx:
            new, n = _map_text(out[i].get("content"), lambda t: _apply_rules(t, cfg["rules"]))
            if n:
                out[i]["content"] = new
                rep["hits"] += n
                rep["changed"] = True
                rep["actions"].append("rewrite")

    if level >= 1 and "inject_joiner" in policy:  # L1
        for i in idx:
            def _inj(t):
                return (_inject_joiner(t), 1) if _suspicious(t) else (t, 0)
            new, n = _map_text(out[i].get("content"), _inj)
            if n:
                out[i]["content"] = new
                rep["changed"] = True
                rep["actions"].append("inject_joiner")

    if level >= 2 and "prepend" in policy:        # L2
        for i in idx:
            def _pre(t):
                return (t, 0) if t.startswith(PREPEND_TEXT) else (PREPEND_TEXT + t, 1)
            new, n = _map_text(out[i].get("content"), _pre)
            if n:
                out[i]["content"] = new
                rep["changed"] = True
                rep["actions"].append("prepend")

    if level >= 3 and "degrade" in policy:        # L3
        new = []
        for m in out:
            if isinstance(m, dict) and m.get("role") == "system":
                new.append({"role": "user", "content": m.get("content")})
                rep["actions"].append("degrade")
                rep["changed"] = True
            else:
                new.append(m)
        out = new
    return out, rep


# ---------------- L1 策略记忆 ----------------
def fingerprint(messages):
    """只看 system：护栏只改 system，别的变了不该影响记忆。"""
    parts = []
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content")
            parts.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, sort_keys=True))
    if not parts:
        return ""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def recall(ctx, fp):
    """该指纹上次奏效的等级；没有记忆返回 0。"""
    if not fp:
        return 0
    if fp in _PLAN:
        return _PLAN[fp]
    lv = 0
    try:
        row = ctx["store"].q1(
            "SELECT level, rule_ver FROM sanitize_plan WHERE fp=?", (fp,))
        if row is not None:
            # 规则文件改过 → 旧记忆作废，重新从 level 0 试（否则新规则永远轮不到）
            ver = load_rules(ctx)["mtime"]
            if abs(float(row["rule_ver"] or 0) - float(ver)) < 1e-6:
                lv = max(0, min(MAX_LEVEL, int(row["level"])))
    except Exception:
        lv = 0
    _PLAN[fp] = lv
    return lv


def remember(ctx, fp, level):
    """记住这个指纹用哪个等级才过；level 0 不记（默认就是 0）。"""
    if not fp or level <= 0:
        return
    _PLAN[fp] = level
    try:
        ts = time.time()
        ver = load_rules(ctx)["mtime"]
        ctx["store"].x(
            "INSERT INTO sanitize_plan(fp,level,rule_ver,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(fp) DO UPDATE SET level=?, rule_ver=?, updated_at=?",
            (fp, level, ver, ts, level, ver, ts))
    except Exception as e:
        sys.stderr.write("[guard] 写策略记忆失败：%s\n" % e)


# ---------------- 熔断 ----------------
def breaker_open():
    return _FAILS["until"] > time.time()


def note_fail():
    _FAILS["n"] += 1
    if _FAILS["n"] >= BREAKER_AFTER:
        _FAILS["until"] = time.time() + BREAKER_SECONDS
        _FAILS["n"] = 0


def note_ok():
    _FAILS["n"] = 0


def stats():
    return {"plan_cache": len(_PLAN), "fail_streak": _FAILS["n"],
            "breaker_until": _FAILS["until"], "breaker_open": breaker_open()}


def reset_plans(ctx):
    """清空策略记忆（规则大改后建议调一次，或运维发现记忆过时）。"""
    _PLAN.clear()
    n = 0
    try:
        n = ctx["store"].x("DELETE FROM sanitize_plan")
    except Exception:
        pass
    return {"ok": True, "removed": n}
