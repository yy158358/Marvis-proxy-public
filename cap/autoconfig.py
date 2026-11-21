# -*- coding: utf-8 -*-
"""cap/autoconfig.py —— 首次启动时自动探测并写入设备指纹与 QIMEI 应用标识。

================ 目标 ================

用户 clone 下来、启动服务，**不需要手动填任何设备相关配置**：

  · `marvis_qimei36`       —— 36 位设备指纹，从本机腾讯 Marvis 客户端取得
  · `MARVIS_QIMEI_APPKEY`  —— QIMEI SDK 应用标识，从客户端 resources 里取得

两个值都**只能从本机客户端提取，不能凭空生成**（服务端会校验）。
探测成功后写入 `data/` 下的缓存文件，并把 appKey 写进 `data/marvis_appkey.txt`，
后续启动直接复用，不再重复探测。

================ 优先级 ================

`config.json` 里显式填写的值 > 环境变量 > `data/` 缓存 > 现场探测 > 留空

现场探测失败时**不会中断启动**，只是把该字段留空，并在日志里给出明确指引。

================ 三个入口 ================

  `ensure(config, ctx)`   —— 启动时调用一次，做「读缓存 → 缺就探测 → 回写」
  `detect_qimei36()`      —— 只探测 qimei36
  `detect_appkey()`       —— 只探测 appKey

没有任何外部依赖，只用标准库 + 本项目的 tools/。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

# 缓存文件名（放在 data_dir 下）
QIMEI36_FILE = "qimei36.txt"
APPKEY_FILE = "marvis_appkey.txt"

# 已知的 QIMEI 应用标识：从客户端 resources\.env 读不到时作为兜底。
# 这是官方客户端使用的值，不是密码，但也不应该被硬编码成唯一来源。
FALLBACK_APPKEY = "0WIN06Y06DKV1IUQ"

# 客户端安装根目录的候选盘符
_DRIVES = "CDEFGHIJ"

# node 可执行文件的候选位置。
# 现实情况：很多机器上并没有系统级安装的 Node.js，但 node 会作为**其他软件的
# 私有运行时**存在（DeepSeek Harness、workbuddy、Code、各种 Electron 应用……）。
# 所以这里除了标准安装位置，还把常见的私有运行时目录也纳入搜索。
_NODE_HINTS = (
    # 标准安装
    r"C:\Program Files\nodejs\node.exe",
    r"%PROGRAMFILES%\nodejs\node.exe",
    r"%LOCALAPPDATA%\Programs\nodejs\node.exe",
    r"%APPDATA%\nvm\current\node.exe",
    r"%USERPROFILE%\.nvm\current\node.exe",
    # 项目自带的便携版（tools/setup_node.py 下载到这里）
    None,  # 运行期填成 <repo>/tools/node/node.exe
    # 常见软件自带的私有运行时
    r"%USERPROFILE%\.workbuddy\binaries\node\versions\*\node.exe",
    r"%LOCALAPPDATA%\Programs\Microsoft VS Code\resources\app\..\..\node.exe",
    r"%LOCALAPPDATA%\Programs\cursor\resources\app\..\..\node.exe",
    r"%PROGRAMFILES%\Microsoft VS Code\node.exe",
    r"%USERPROFILE%\scoop\apps\nodejs\current\node.exe",
    r"%PROGRAMDATA%\chocolatey\bin\node.exe",
    # POSIX
    "/usr/bin/node", "/usr/local/bin/node", "/opt/homebrew/bin/node",
    os.path.expanduser("~/.nvm/versions/node/*/bin/node"),
)


def _node_candidates():
    """返回 node 候选路径列表（已展开变量，项目中自带的排在最前）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = [os.path.join(root, "tools", "node", "node.exe"),
           os.path.join(root, "tools", "node", "bin", "node")]
    for h in _NODE_HINTS:
        if h:
            out.append(os.path.expandvars(os.path.expanduser(h)))
    return out


def find_node():
    """找 node 可执行文件。找不到返回 ""。"""
    env = (os.environ.get("QIMEI_NODE") or "").strip()
    if env and os.path.exists(env):
        return env
    p = shutil.which("node") or shutil.which("node.exe")
    if p:
        return p
    for h in _node_candidates():
        if os.path.exists(h):
            return h
        # 支持通配符（nvm / workbuddy 版本目录）
        if "*" in h:
            hits = sorted(glob.glob(h), reverse=True)
            if hits:
                return hits[0]
    return ""


def _log(msg):
    sys.stderr.write("[autoconfig] %s\n" % msg)
    sys.stderr.flush()


def _marvis_app_dirs():
    """列出所有 <盘>:\\Program Files\\Tencent\\Marvis\\Application\\<ver>\\win32-native。"""
    out = []
    for d in _DRIVES:
        for sub in ("Program Files", "Program Files (x86)"):
            app = os.path.join("%s:/%s" % (d, sub), "Tencent", "Marvis", "Application")
            app = app.replace("/", os.sep)
            if not os.path.isdir(app):
                continue
            try:
                versions = sorted(os.listdir(app), reverse=True)
            except OSError:
                continue
            for v in versions:
                native = os.path.join(app, v, "win32-native")
                if os.path.isdir(native):
                    out.append(native)
    return out


def _data_dir(config, ctx):
    base = (ctx or {}).get("data_dir") or (config or {}).get("data_dir")
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return base


def _read(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write(path, value):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(value + "\n")
        return True
    except Exception as e:
        _log("写入 %s 失败: %r" % (path, e))
        return False


# ------------------------------------------------------------------ qimei36
# 客户端把真实设备指纹写在自己的本地数据里，形如：
#     marvis_client"guid" <32 位 guid>"qimei36"$<36 位十六进制>"a153"$...
# 也见过纯文本形式：
#     qimei36" <36 位十六进制>"
# 这条路径**完全不需要 node**，只要能读到客户端数据文件即可。
_LOGRX_U8 = re.compile(r'qimei36\W{0,3}([0-9a-f]{36})\b', re.I)
_LOGRX_DOLLAR = re.compile(r'qimei36"\$?([0-9a-f]{36})', re.I)
_GUIDRX = re.compile(r'guid\W{0,3}([0-9a-f]{32})', re.I)


def _client_log_dirs():
    """客户端日志可能存放的目录。"""
    out = []
    for env in ("APPDATA", "LOCALAPPDATA", "TEMP"):
        base = os.environ.get(env)
        if base:
            out.append(os.path.join(base, "Tencent", "Marvis"))
    return out


# 这些文件名值得优先看（客户端把设备指纹写进了 IndexedDB 的 LevelDB 里）
_LOG_NAME_HINT = re.compile(r"(\.log$|\.txt$|\.ldb$|\.sst$|\.json$|MANIFEST|CURRENT)", re.I)


def _iter_log_files(limit=200):
    """递归收集客户端目录下可能含 qimei36 的文件，大的优先。"""
    seen = []
    for d in _client_log_dirs():
        if not os.path.isdir(d):
            continue
        for root, dirs, files in os.walk(d):
            # 跳过体积巨大的缓存目录，省时间
            dirs[:] = [x for x in dirs
                       if x.lower() not in ("cache", "gpucache", "code cache",
                                            "blob_storage", "service worker")]
            for fn in files:
                if not _LOG_NAME_HINT.search(fn):
                    continue
                p = os.path.join(root, fn)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                if sz <= 0 or sz > 32 * 1024 * 1024:
                    continue
                seen.append((sz, p))
    seen.sort(reverse=True)
    return [p for _, p in seen[:limit]]


def _scan_log_file(path, want_guid=""):
    """从一个文件里找 qimei36。返回 (value, guid)。

    文件可能是纯文本日志，也可能是 LevelDB 的 .ldb（含二进制前缀），
    所以按 bytes 读再解码，逐行找关键字。
    若给了 want_guid，只认同一行里 guid 匹配的记录；
    否则返回出现次数最多的那个值（多设备机器上会有好几个）。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return "", ""
    text = raw.decode("utf-8", "replace")
    if "qimei36" not in text:
        return "", ""
    hits = {}
    for line in text.splitlines():
        if "qimei36" not in line:
            continue
        m = _LOGRX_U8.search(line) or _LOGRX_DOLLAR.search(line)
        if not m:
            continue
        val = m.group(1).lower()
        g = _GUIDRX.search(line)
        guid = (g.group(1).lower() if g else "")
        if want_guid and guid and guid != want_guid:
            continue
        hits[val] = hits.get(val, 0) + 1
    if not hits:
        return "", ""
    best = max(hits.items(), key=lambda kv: kv[1])[0]
    return best, want_guid


def detect_qimei36_from_logs():
    """从客户端本地文件里读出 qimei36（不需要 node）。

    这是首选路径：只要客户端跑过，它的 IndexedDB / 日志里就有明文值。
    返回 36 位字符串，找不到返回 ""。
    """
    for p in _iter_log_files():
        val, _ = _scan_log_file(p)
        if val:
            _log("从客户端本地文件取到 qimei36：%s" % os.path.basename(p))
            return val
    return ""


def detect_qimei36_via_node():
    """调客户端自带的 qimei_napi.node 取 QIMEI36（需要 node）。

    日志里拿不到时的兜底。成功后返回 36 位字符串，失败返回 ""。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = os.path.join(root, "tools", "qimei36.js")
    if not os.path.exists(js):
        return ""

    node = find_node()
    if not node:
        _log("未找到 node，跳过 node 通道的 qimei36 探测。")
        _log("  修复：装 Node.js，或设置环境变量 QIMEI_NODE 指向 node 可执行文件。")
        return ""

    dirs = _marvis_app_dirs()
    if not dirs:
        _log("未找到 Marvis 客户端，跳过 qimei36 探测")
        return ""

    # 用最新版本目录的 qimei.dll
    dll = os.path.join(dirs[0], "qimei.dll")
    ver = os.path.basename(os.path.dirname(dirs[0]))
    cmd = [node, js, "--native-dir", dirs[0], "--app-version", ver]
    if os.path.exists(dll):
        cmd += ["--dll", dll]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
    except Exception as e:
        _log("调用 qimei36.js 失败: %r" % e)
        return ""
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        _log("qimei36 探测失败 rc=%s: %s" % (r.returncode, (r.stderr or "").strip()[:200]))
        return ""
    try:
        info = json.loads(out.splitlines()[-1])
    except Exception:
        _log("qimei36 输出无法解析: %s" % out[:200])
        return ""
    q36 = (info.get("qimei36") or "").strip()
    if len(q36) != 36:
        _log("qimei36 长度异常: %r" % q36)
        return ""
    return q36


# ------------------------------------------------------------------ appKey
_APPKEY_RX = re.compile(r"^\s*MARVIS_QIMEI_APPKEY\s*=\s*(\S+)", re.M)


def detect_appkey():
    """从客户端 resources\\.env 读取 MARVIS_QIMEI_APPKEY。

    顺序：环境变量 > 各版本 resources\\.env > FALLBACK_APPKEY。
    """
    env = (os.environ.get("MARVIS_QIMEI_APPKEY") or "").strip()
    if env:
        return env

    for native in _marvis_app_dirs():
        # native = <app>\<ver>\win32-native，往上两级到 <app>\<ver>
        ver_dir = os.path.dirname(native)
        for rel in (os.path.join("resources", ".env"), ".env"):
            p = os.path.join(ver_dir, rel)
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8-sig", errors="replace") as f:
                    m = _APPKEY_RX.search(f.read())
            except Exception:
                continue
            if m:
                return m.group(1).strip()
    return FALLBACK_APPKEY


# ------------------------------------------------------------------ 总入口
def ensure(config, ctx=None):
    """启动时调用一次：补齐 qimei36 与 appKey。

    返回 dict：{"qimei36": str, "appkey": str, "wrote": [...]}
    就地修改 config（内存中的那份），让 cap/device.py 立刻能用上。
    """
    config = config if config is not None else {}
    base = _data_dir(config, ctx)
    result = {"qimei36": "", "appkey": "", "wrote": []}

    # ---- qimei36：config 显式值优先
    q36 = (config.get("marvis_qimei36") or "").strip()
    if not q36:
        cached = _read(os.path.join(base, QIMEI36_FILE))
        if len(cached) == 36:
            q36 = cached
        else:
            # ① 先读客户端日志（不需要任何额外运行时）
            q36 = detect_qimei36_from_logs()
            # ② 日志里没有，再试 node 通道
            if not q36:
                q36 = detect_qimei36_via_node()
            if q36 and _write(os.path.join(base, QIMEI36_FILE), q36):
                result["wrote"].append(QIMEI36_FILE)
    if q36:
        # 写回 config，cap/device.py 的 _qimei36() 第一优先级就是它
        config["marvis_qimei36"] = q36
    else:
        _log("qimei36 未取得（不影响启动，请求可能被判风控）。三种补法：")
        _log("  1) 手动填 config.json 的 marvis_qimei36")
        _log("  2) 在客户端日志里搜 qimei36，把 36 位值抄过来")
        _log("  3) 运行 python tools/qimei36_fetch.py（需要 node）")
    result["qimei36"] = q36

    # ---- appKey：环境变量 > 缓存 > 探测
    # 这里要分清「读出来的」和「兜底的」：FALLBACK_APPKEY 是所有人共用的内置值，
    # 不是探测结果。早年把它当探测结果写进缓存并打印「已自动写入」，用户会以为
    # 那是针对自己机器测出来的 —— 日后上游换 key，他抱着一个来路不明的缓存排查。
    appkey = (os.environ.get("MARVIS_QIMEI_APPKEY") or "").strip()
    if not appkey:
        cached = _read(os.path.join(base, APPKEY_FILE))
        if cached:
            appkey = cached
            result["appkey_source"] = "cached"
        else:
            appkey = detect_appkey()
            if appkey == FALLBACK_APPKEY:
                # 兜底常量不落盘：写下来只会变成下一次的「缓存」，把来路彻底抹掉。
                result["appkey_source"] = "builtin"
            elif appkey and _write(os.path.join(base, APPKEY_FILE), appkey):
                result["wrote"].append(APPKEY_FILE)
                result["appkey_source"] = "detected"
    else:
        result["appkey_source"] = "env"
    result["appkey"] = appkey
    # 供 cap/uskey.py / tools 读取
    if appkey:
        os.environ.setdefault("MARVIS_QIMEI_APPKEY", appkey)

    return result
