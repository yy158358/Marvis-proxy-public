# -*- coding: utf-8 -*-
"""全面自检：静态 + 接口 + 少量真实调用。每项输出 PASS/FAIL/WARN 与证据。

根目录取本文件所在目录的上一级；API Key 从环境变量 `MARVIS_KEY` 或
`data/client_keys.txt` 读（都没有时按「表里没 key = 不校验」的旧行为跑）。
用法： py -3 tools\\selfcheck.py [--base http://127.0.0.1:8088]
"""
import json, os, re, sqlite3, subprocess, sys, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def _data_dir():
    """和 serve.py 同一套规则解析数据目录：``MARVIS_DATA_DIR`` > config.json 的
    ``data_dir`` > ``data``，相对路径按工程根算。

    以前这里写死 ``data/marvis.db``，跑过 ``MARVIS_DATA_DIR=...`` 的实例再自检，
    查的是**另一个**库：要么报库不存在，要么更糟 —— 对着空库夸一切正常。
    """
    d = (os.environ.get("MARVIS_DATA_DIR") or "").strip()
    if not d:
        try:
            with open(os.path.join(ROOT, "config.json"), encoding="utf-8-sig") as f:
                d = (json.load(f).get("data_dir") or "").strip()
        except Exception:
            d = ""
    d = d or "data"
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


DATA = _data_dir()
DB = os.path.join(DATA, "marvis.db")
FILES = os.path.join(DATA, "files")
# 日志目录在 serve.py 里是固定的 ROOT/logs（不受 data_dir 影响），这里照抄。
LOG_PATH = os.path.join(ROOT, "logs", "serve.log")


def _arg(flag, default=""):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


BASE = _arg("--base", "http://127.0.0.1:8088").rstrip("/")


def _find_key():
    """取一把可用的 key。

    文件里可以有多行：一行一把，`#` 开头是注释，空行忽略。以前这里直接
    `.read().strip()` 读整份文件，一旦有人为了记事多写一行注释，整份内容就
    变成一个两百多字符的「key」，发出去必然 401 —— 而文件本身看着没问题。
    """
    k = os.environ.get("MARVIS_KEY", "").strip()
    if k:
        return k
    p = os.path.join(DATA, "client_keys.txt")
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return ""
    for line in lines:
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        # 一行里可能写成 "mvk_xxx  # 备注"
        t = t.split("#", 1)[0].strip()
        if t:
            return t
    return ""


KEY = _find_key()
results = []
def rec(name, ok, detail="", todo=False, need_key=False):
    """记账一行。
    todo=True    环境还没就绪，这轮没法验（SKIP）
    need_key=True 这条要一把真 key 才能验；没有 key 也是 SKIP，不是失败
    两者都不算通过也不懂失败，免得新装一份看着满屏红。
    """
    if todo or (need_key and not KEY):
        tag = "SKIP"
        if need_key and not KEY and "没有可用 key" not in detail:
            detail = (detail + "  " if detail else "") + "没有可用 key（见 README「自检前先准备 key」）"
    else:
        tag = "PASS" if ok is True else ("WARN" if ok == "warn" else "FAIL")
    results.append((tag, name, detail))
    print("%-5s %-46s %s" % (tag, name, detail))

def req(path, method="GET", body=None, headers=None, timeout=90, raw=False):
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    t = time.time()
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        payload = resp.read()
        return resp.status, dict(resp.headers), payload, int((time.time()-t)*1000)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), int((time.time()-t)*1000)
    except Exception as e:
        return 0, {}, str(e).encode(), int((time.time()-t)*1000)

print("=" * 78)
print("A. 静态检查")
print("=" * 78)

# A1 编译
files = []
for d in (".", "core", "cap", "api"):
    for f in sorted(os.listdir(d)):
        if f.endswith(".py"): files.append(os.path.join(d, f))
bad = []
for f in files:
    r = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True)
    if r.returncode: bad.append((f, r.stderr.decode()[:120]))
rec("A1 全部 .py 可编译（%d 个）" % len(files), not bad, str(bad[:2]) if bad else "")

# A2 config 键交叉
cfg = json.load(open("config.json", encoding="utf-8"))
code = ""
for f in files: code += open(f, encoding="utf-8").read() + "\n"
used = set(re.findall(r'(?:config|cfg|ctx\["config\"])\.get\(\s*["\']([a-z_0-9]+)["\']', code))
used |= set(re.findall(r'\[\s*["\']config["\']\s*\]\.get\(\s*["\']([a-z_0-9]+)["\']', code))
used |= set(re.findall(r'config\.([a-z_0-9]+)\b(?!\s*\()', code)) - {"json", "get", "setdefault", "py", "toml", "yaml", "yml"}
used |= set(re.findall(r'config\[\s*["\']([a-z_0-9]+)["\']\s*\]', code))
# 不带 config 变量名、但确实是读配置文件的写法，两种：
#   json.load(open("config.json")).get("k")   —— 一行写完
#   with open(cfg_path) as f: json.load(f).get("k")  —— 先用 with 打开，靠 CROSS 白名单兜
used |= set(re.findall(
    r'json\.load\(\s*open\([^)]*config\.json[^)]*\)[^)]*\)\.get\(\s*["\']([a-z_0-9]+)["\']', code))
# 跨语句读法：变量名出现在代码里且邻近 config 字样，靠人工白名单保证不漏（见 CROSS）
#   uskey_backend          —— cap/uskey.py 从外部 config 对象取
#   uskey_ctx_getter_rva   —— cap/uskey.py:_resolve_ctx_getter_rva 用 with open(cfg) 读
CROSS = {"uskey_backend", "uskey_ctx_getter_rva"}
used |= {k for k in CROSS if re.search(r'["\']%s["\']' % k, code)}
DYNAMIC = {"host", "port", "data_dir", "admin_token", "image_workers"}  # load_config 用元组循环 + setdefault 处理
orphan = sorted(k for k in cfg if k not in used and k not in DYNAMIC)
missing = sorted(k for k in used if k not in cfg)
rec("A2 config 无孤儿键", not orphan, "孤儿: " + ", ".join(orphan) if orphan else "共 %d 键全部被引用" % len(cfg))
rec("A2b 代码引用的键都在 config.json", not missing, "缺失: " + ", ".join(missing) if missing else "")

# A3 仓库必备文件
docs = ["README.md", "SECURITY.md", "LICENSE"]
for d in docs:
    if os.path.exists(d):
        rec("A3 文档 %s" % d, True, "%d 行" % sum(1 for _ in open(d, encoding="utf-8")))
    else:
        rec("A3 文档 %s" % d, False, "不存在")

# A4 DB schema
if os.path.exists(DB):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    want = {"accounts", "tasks", "usage", "login_pending", "client_keys", "sessions", "sanitize_plan"}
    rec("A4 数据表齐全", want <= tables, "缺: " + ",".join(sorted(want - tables)) if not want <= tables else str(len(tables)) + " 张表")
else:
    rec("A4 数据表齐全", True, "跳过：%s 不存在（还没启动过服务，先跑一次 run）" % DB, todo=True)

# A5 护栏规则文件
if os.path.exists("sanitize.json"):
    sj = json.load(open("sanitize.json", encoding="utf-8"))
    rec("A5 sanitize.json 可解析", True, "rules=%d policy=%s" % (len(sj.get("rules") or []), sj.get("policy")))
else:
    rec("A5 sanitize.json 可解析", False, "文件不存在")

print()
print("=" * 78)
print("B. 本地接口（不耗上游额度）")
print("=" * 78)
st, _, body, ms = req("/health")
h = json.loads(body) if st == 200 else {}
rec("B1 GET /health", st == 200 and h.get("ok") is True, "accounts=%s" % (h.get("accounts"),))
rec("B1b /health 有 expired 计数", "expired" in (h.get("accounts") or {}), str(h.get("accounts")))
rec("B1c /health 有 accounts_expired", "accounts_expired" in h, "=%s" % h.get("accounts_expired"))

st, _, body, _ = req("/v1/models")
models = json.loads(body).get("data", []) if st == 200 else []
rec("B2 GET /v1/models", st == 200 and len(models) >= 5, "%d 个模型" % len(models))
rec("B2b 每个模型有 caps/type", all("capabilities" in m and "type" in m for m in models),
    "" if models else "无模型")

for path, tag in [("/admin/accounts", "B3 /admin/accounts"), ("/admin/usage?days=1&by=day", "B4 /admin/usage 报表"),
                  ("/admin/usage", "B4b /admin/usage 明细"), ("/admin/wallet", "B5 /admin/wallet"),
                  ("/admin/sanitize", "B6 /admin/sanitize"), ("/admin/keys", "B7 /admin/keys"),
                  ("/admin/backups", "B8 /admin/backups")]:
    st, _, body, ms = req(path)
    rec(tag, st == 200, "HTTP %d %dms" % (st, ms))

# 错误分支
st, _, body, _ = req("/v1/chat/completions", "POST", {"model": "deepseek-v4-1-flash-261017",
                                                     "messages": [{"role": "user", "content": "1"}]}, timeout=30)
rec("B9 无 key → 401", st == 401, "HTTP %d" % st)
st, _, body, _ = req("/v1/chat/completions", "POST", {"model": "nope", "messages": [{"role": "user", "content": "1"}]},
                     {"X-API-Key": KEY}, timeout=30)
rec("B10 未知模型 → 404", st == 404, "HTTP %d %s" % (st, body[:60]), need_key=True)
st, _, body, _ = req("/v1/images/generations", "POST", {"prompt": "x", "size": "1280x1280"}, {"X-API-Key": KEY}, timeout=30)
rec("B11 1280 尺寸 → 400", st == 400, "HTTP %d %s" % (st, body[:60]), need_key=True)
st, _, body, _ = req("/v1/images/tasks/img_nonexist", None or "GET", None, {"X-API-Key": KEY}, timeout=30)
rec("B12 未知任务 → 404", st == 404, "HTTP %d" % st, need_key=True)
st, _, body, _ = req("/files/../config.json", headers={"X-API-Key": KEY}, timeout=30)
rec("B13 /files 路径穿越被拦", st in (400, 404), "HTTP %d" % st, need_key=True)

# key 校验覆盖到产物与计数端点（这两处以前是裸的）
st, _, body, _ = req("/v1/images/tasks/img_nonexist", timeout=30)
rec("B15 /v1/images/tasks 无 key → 401", st == 401, "HTTP %d" % st)
st, _, body, _ = req("/v1/messages/count_tokens", "POST", {"messages": [{"role": "user", "content": "1"}]}, timeout=30)
rec("B16 count_tokens 无 key → 401", st == 401, "HTTP %d" % st)
st, _, body, _ = req("/v1/messages/count_tokens", "POST",
                     {"messages": [{"role": "user", "content": "1"}]}, {"X-API-Key": KEY}, timeout=30)
rec("B16b count_tokens 带 key → 200", st == 200, "HTTP %d" % st, need_key=True)
st, _, body, _ = req("/files/nonexistent.png", timeout=30)
rec("B17 /files 无 key → 401", st == 401, "HTTP %d" % st)
st, _, body, _ = req("/files/nonexistent.png?key=" + KEY, timeout=30)
rec("B17b /files ?key= → 404（绕过鉴权后正常找不到）", st == 404, "HTTP %d" % st, need_key=True)

# /files Range（用已有产物）
_fs = os.listdir(FILES) if os.path.isdir(FILES) else []
png = [f for f in _fs if f.endswith(".png")]
if png:
    size = os.path.getsize(os.path.join(FILES, png[0]))
    st, hdrs, body, _ = req("/files/" + png[0], headers={"Range": "bytes=0-99", "X-API-Key": KEY}, timeout=30)
    ok = st == 206 and len(body) == 100 and hdrs.get("Content-Range", "").endswith("/%d" % size)
    rec("B14 /files Range=bytes=0-99 → 206", ok, "HTTP %d len=%d cr=%s" % (st, len(body), hdrs.get("Content-Range")), need_key=True)

print()
print("=" * 78)
print("C. 真实上游调用（4 次）")
print("=" * 78)
CK = {"X-API-Key": KEY}
st, _, body, ms = req("/v1/chat/completions", "POST",
    {"model": "deepseek-v4-1-flash-261017", "messages": [{"role": "user", "content": "回两个字"}], "max_tokens": 512}, CK)
ok = st == 200
try:
    d = json.loads(body); txt = d["choices"][0]["message"].get("content") or ""
    mv = d.get("_marvis") or {}
except Exception:
    txt, mv = "", {}
rec("C1 非流式对话", ok, "HTTP %d %r account=%s" % (st, txt[:12], mv.get("account")), need_key=True)

st, hdrs, body, ms = req("/v1/chat/completions", "POST",
    {"model": "deepseek-v4-1-flash-261017", "messages": [{"role": "user", "content": "回两个字"}],
     "max_tokens": 512, "stream": True}, CK)
sse = body.decode("utf-8", "replace")
rec("C2 流式对话", st == 200 and "text/event-stream" in hdrs.get("Content-Type", ""),
    "HTTP %d ctype=%s blocks=%d" % (st, hdrs.get("Content-Type"), sse.count("data:")), need_key=True)

tools = [{"type": "function", "function": {"name": "get_weather", "description": "查天气",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
st, _, body, _ = req("/v1/chat/completions", "POST",
    {"model": "deepseek-v4-1-flash-261017", "messages": [{"role": "user", "content": "北京天气"}],
     "tools": tools, "tool_choice": "auto", "max_tokens": 1024}, CK)
try:
    d = json.loads(body); fr = d["choices"][0].get("finish_reason")
    tc = d["choices"][0]["message"].get("tool_calls")
except Exception:
    fr, tc = None, None
rec("C3 tools 调用", st == 200 and fr == "tool_calls" and bool(tc),
    "HTTP %d finish=%s name=%s" % (st, fr, (tc[0]["function"]["name"] if tc else None)), need_key=True)

st, _, body, _ = req("/v1/messages", "POST",
    {"model": "claude-3-5-sonnet-20241022", "max_tokens": 512,
     "messages": [{"role": "user", "content": "回两个字"}]}, CK)
try:
    d = json.loads(body); stop = d.get("stop_reason"); typ = d.get("type")
except Exception:
    stop, typ = None, None
rec("C4 Anthropic /v1/messages", st == 200 and typ == "message", "HTTP %d type=%s stop=%s" % (st, typ, stop), need_key=True)

st, _, body, _ = req("/v1/images/generations", "POST", {"prompt": "一个红色圆形", "size": "512x512"}, CK, timeout=40)
try:
    d = json.loads(body); tid = d.get("id"); tst = d.get("status")
except Exception:
    tid, tst = None, None
rec("C5 生图提交（异步）", st == 202 and bool(tid), "HTTP %d id=%s status=%s" % (st, tid, tst), need_key=True)

print()
print("=" * 78)
print("D. 运行时状态")
print("=" * 78)
if os.path.exists(DB):
    c2 = sqlite3.connect(DB); c2.row_factory = sqlite3.Row
    accs = [dict(r) for r in c2.execute("select id,status,weight,cooldown_until,expires_in,note from accounts")]
    rec("D1 账号池", all(a["status"] == "active" for a in accs),
        " ".join("#%s:%s/w%s" % (a["id"], a["status"], a["weight"]) for a in accs))
else:
    rec("D1 账号池", True, "跳过：%s 不存在（还没启动过服务）" % DB, todo=True)
if os.path.exists(LOG_PATH):
    _log = open(LOG_PATH, encoding="utf-8", errors="replace").read()
else:
    _log = ""
    rec("D2/D3/D4/D5/D6 日志检查", True, "跳过：%s 不存在（还没启动过服务）" % LOG_PATH, todo=True)
logtail = _log[_log.rfind("marvis-proxy listening"):]  # 只看本次启动之后
logall = _log
if _log:
    for pat, tag in [("[cleanup]", "D2 清理线程跑过"), ("[refresh-ahead]", "D3 提前续期跑过")]:
        rec(tag, pat in logall, "累计 %d 次" % logall.count(pat))
# 这三条是「出问题时才出现」的自愈日志：没有 403/401/429 的平静期它们是空的，属正常
_recent_risk = sum(logtail.count(k) for k in ("4100404", "4100406", "4100403", " 429 ", "[throttle]"))
for pat, tag in [("[device]", "D4 设备头自愈日志"), ("[guard]", "D4b 护栏升级日志"),
                 ("[failover]", "D4c 换号日志"), ("[throttle]", "D4d 429 退避日志")]:
    if not _log:
        rec(tag, True, "跳过：没有日志文件", todo=True)
        continue
    n = logall.count(pat)
    if n:
        rec(tag, True, "累计 %d 次" % n)
    else:
        rec(tag, "warn" if _recent_risk == 0 else False,
            "未触发（本次启动后上游 4xx 标记 %d 处；平静期为正常）" % _recent_risk)
if _log:
    # 客户端中途断开不是服务故障：流式回答写到一半用户关页面/按 Esc，
    # socketserver 就会抛 ConnectionAbortedError(10053) / ConnectionResetError(10054)。
    # 这类 Traceback 只说明「对方先走了」，不该把 D5 判红。
    _BENIGN = ("ConnectionAbortedError", "ConnectionResetError", "BrokenPipeError",
               "WinError 10053", "WinError 10054", "WinError 10058")
    _tb = [i for i, line in enumerate(logtail.splitlines()) if "Traceback" in line]
    _real = 0
    _benign = 0
    _loglines = logtail.splitlines()
    for i in _tb:
        block = "\n".join(_loglines[i:i + 30])
        if any(k in block for k in _BENIGN):
            _benign += 1
        else:
            _real += 1
    _d5d = "Traceback 出现 %d 次" % len(_tb)
    if _benign:
        _d5d += "（其中 %d 次为客户端断开，属正常）" % _benign
    rec("D5 无未处理异常", _real == 0, _d5d)
    rec("D6 无 500", '" 500 -' not in logtail, "500 出现 %d 次" % logtail.count('" 500 -'))

print()
print("=" * 78)
p = len([r for r in results if r[0] == "PASS"]); w = len([r for r in results if r[0] == "WARN"])
f = len([r for r in results if r[0] == "FAIL"]); s = len([r for r in results if r[0] == "SKIP"])
print("汇总：PASS %d / WARN %d / FAIL %d%s" % (p, w, f, (" / SKIP %d" % s) if s else ""))
if f:
    print("\n失败项：")
    for tag, name, detail in results:
        if tag == "FAIL": print("  ✗", name, "|", detail)
if s:
    print("\n跳过项（环境未就绪，不算失败）：")
    for tag, name, detail in results:
        if tag == "SKIP": print("  ·", name, "|", detail)
