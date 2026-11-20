# -*- coding: utf-8 -*-
"""本机（Android 版 libqimei.so）出 uskey 票据。

背景：PC 端用 `beacon_napi.node` 出票（392 字节），手机端用 `lib/arm64-v8a/libqimei.so`
出票（248 字节），两者格式不同但**都能被上游接受**——2026-11-18 实测：
本机出票 + 真 qimei36 + 账号自身的 guid → `/v1/chat/completions` 返回 200。

之所以要本机出票：容器里没有 Windows，跑不了 beacon_napi.node。

依赖：
- `libqimei_patched.so`：把 `.gnu.version` 表改成全局索引后的 libqimei（见 patch_versym.py）
- `libjni_stub.so`：假 JNIEnv，用来调 Java 层注册的 native `uk(5×String)`
- 垫片目录（libc.so / libm.so / libdl.so / libandroid.so），由 build.sh 生成

出票参数（实测确认，顺序不能变）：
    uk(appkey, qimei36, guid, busid, bus_info)
"""
import ctypes
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 为什么需要「另找一个目录」：/sdcard 是 FUSE(fuseblk)，.so 的 PT_LOAD 段没法 mmap 进去，
# dlopen 会报 "failed to map segment from shared object"；因此必须落到真实文件系统上。
# 目录不写死：MARVIS_SHIM_DIR > config.shim_dir > /root/marvis-shim > $HOME/.marvis-shim > 项目内。
_CANDIDATE_DIRS = [
    os.environ.get("MARVIS_SHIM_DIR", ""),
    "/root/marvis-shim",
    os.path.join(os.path.expanduser("~"), ".marvis-shim"),
    os.path.join(ROOT, "tools", "bionic_shim"),   # 最后手段：FUSE 上会失败，但至少路径存在
]
_CFG_SHIM_DIR = {"v": ""}     # 由 serve 启动时 set_config_dir() 注入
_FILE_SHIM_DIR = {"v": None}  # 兜底：自己读 config.json（不依赖 serve 是否注入）


def _cfg_file_shim_dir():
    if _FILE_SHIM_DIR["v"] is not None:
        return _FILE_SHIM_DIR["v"]
    v = ""
    try:
        with open(os.path.join(ROOT, "config.json")) as f:
            v = str((json.load(f) or {}).get("shim_dir") or "").strip()
    except Exception:
        v = ""
    _FILE_SHIM_DIR["v"] = v
    return v


def set_config_dir(d):
    """config.json 里的 shim_dir；serve.py 载入配置后调一次。"""
    if d:
        _CFG_SHIM_DIR["v"] = str(d).strip()


def _pick_dir():
    cands = []
    if _CFG_SHIM_DIR["v"]:
        cands.append(_CFG_SHIM_DIR["v"])
    elif _cfg_file_shim_dir():
        cands.append(_cfg_file_shim_dir())
    cands.extend(_CANDIDATE_DIRS)
    for d in cands:
        if d and os.path.exists(os.path.join(d, "libqimei_patched.so")):
            return d
    return os.path.join(ROOT, "tools", "bionic_shim")


SHIM_DIR = _pick_dir()
LIB_PATH = os.path.join(SHIM_DIR, "libqimei_patched.so")
JNI_PATH = os.path.join(SHIM_DIR, "libjni_stub.so")
APP_KEY = "0WIN06Y06DKV1IUQ"
APP_VER = "1.60.3400.35"   # 只影响 init，不改变票长
BUSID = "marvis"

_LOCK = threading.Lock()
_STATE = {"lib": None, "stub": None, "fn": 0, "env": 0, "err": ""}


def _refresh_paths():
    """config 注入后重算一次路径（启动早期可能还没读到 config）。"""
    global SHIM_DIR, LIB_PATH, JNI_PATH
    d = _pick_dir()
    if d != SHIM_DIR:
        SHIM_DIR = d
        LIB_PATH = os.path.join(d, "libqimei_patched.so")
        JNI_PATH = os.path.join(d, "libjni_stub.so")
        _STATE.update(lib=None, stub=None, fn=0, env=0, err="")
    return SHIM_DIR


def _base_addr(path):
    """从 /proc/self/maps 里取 .so 的加载基址（出票函数是未导出符号，按 RVA 调）。"""
    try:
        with open("/proc/self/maps") as f:
            for line in f:
                if path in line:
                    return int(line.split("-")[0], 16)
    except Exception:
        pass
    return 0


_LD_AT_IMPORT = os.environ.get("LD_LIBRARY_PATH") or ""   # 进程启动时的值快照


def _ld_path_ok():
    """**启动时** LD_LIBRARY_PATH 里就有 SHIM_DIR 吗。

    注意：glibc/bionic 的动态链接器只在进程启动时读一次 LD_LIBRARY_PATH，
    之后改 os.environ 对 dlopen 的依赖搜索**没有作用**；所以这里看的是导入时的快照，
    而不是当前 environ（当前值里哪怕有 SHIM_DIR 也不代表链接器认它）。
    """
    return SHIM_DIR in [p for p in _LD_AT_IMPORT.split(":") if p]


def _ensure_ld_path():
    """把 SHIM_DIR 塞到 LD_LIBRARY_PATH 最前面（尽力而为，给子进程用）。

    踩过的坑（2026-11-19）：原来写的是 `os.environ.setdefault(...)`，一旦宿主已经设过
    LD_LIBRARY_PATH（DSH 容器里默认指向它自己的 arm64 lib 目录），setdefault 就什么都不做，
    shim 目录永远进不去 → libqimei_patched.so 的 DT_NEEDED `libandroid.so` 找不到 →
    出票返回 ""，然后 cap/device.py 退回随机 uskey，**每个对话请求必然 403 4100404**。
    现在：① 无条件前置（子进程/重启后能用上）；② 真正的解法是 _preload() 按绝对路径
    RTLD_GLOBAL 预加载依赖，让链接器按 soname 认领，不依赖这条环境变量。
    """
    cur = os.environ.get("LD_LIBRARY_PATH") or ""
    parts = [p for p in cur.split(":") if p]
    if SHIM_DIR not in parts:
        os.environ["LD_LIBRARY_PATH"] = ":".join([SHIM_DIR] + parts)
    return _ld_path_ok()


# 加载顺序 = libqimei_patched.so 的 DT_NEEDED 顺序；按绝对路径 RTLD_GLOBAL 预加载，
# 让加载器按 soname 认领这些依赖，从而**不依赖 LD_LIBRARY_PATH**。
# 前提：libandroid.so 的 soname 必须真的是 `libandroid.so`
#（原来是 libc.so 的副本，soname 是 libc.so，永远认领不上这个名字 —— 见 build.sh）。
PRELOAD = ("libc.so", "libm.so", "libdl.so", "libandroid.so")


def _preload(d):
    for n in PRELOAD:
        p = os.path.join(d, n)
        if os.path.exists(p):
            ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)


def _load():
    if _STATE["lib"] is not None:
        return True
    if os.name != "posix":
        # Windows/PC：没有 Android 版 libqimei，走 cap/uskey.py 的 beacon_napi.node 通道
        _STATE["err"] = "非 POSIX 平台：本机 .so 出票不适用，应改用 beacon_napi.node"
        return False
    _refresh_paths()
    if not (os.path.exists(LIB_PATH) and os.path.exists(JNI_PATH)):
        _STATE["err"] = "缺少 libqimei_patched.so 或 libjni_stub.so（shim 目录 %s）" % SHIM_DIR
        return False
    try:
        _ensure_ld_path()
        _preload(SHIM_DIR)
        lib = ctypes.CDLL(LIB_PATH)
        stub = ctypes.CDLL(JNI_PATH)
        stub.make_env2.restype = ctypes.c_void_p
        stub.call_uk.restype = ctypes.c_void_p
        stub.call_uk.argtypes = [ctypes.c_void_p] * 2 + [ctypes.c_char_p] * 5
        stub.get_last.restype = ctypes.c_char_p
        base = _base_addr(LIB_PATH)
        if not base:
            _STATE["err"] = "取不到加载基址"
            return False
        _STATE.update(lib=lib, stub=stub, fn=base + 0xEF454, env=stub.make_env2(), err="")
        return True
    except Exception as e:      # pragma: no cover
        msg = "%s: %s" % (type(e).__name__, e)
        if "map segment" in msg:
            msg += "（%s 不能 dlopen：多半是 FUSE/sdcardfs 目录，执行 sh run.sh start 让它自举到真实文件系统）" % SHIM_DIR
        elif "cannot open shared object" in msg:
            msg += ("（%s 里没人认领该依赖：libandroid.so 的 soname 必须真是 libandroid.so，"
                    "重建见 tools/bionic_shim/build.sh）" % SHIM_DIR)
        _STATE["err"] = msg
        if not _STATE.get("warned"):
            _STATE["warned"] = True
            sys.stderr.write("[uskey] 本机出票不可用 → 退回随机 uskey（上游必然 403 4100404）：%s\n" % msg)
            sys.stderr.flush()
        return False


def qimei36(config=None, ctx=None):
    """真 QIMEI36：config.marvis_qimei36 > <data_dir>/qimei36.txt > 环境变量。"""
    v = ((config or {}).get("marvis_qimei36") or "").strip()
    if v:
        return v
    env = (os.environ.get("MARVIS_QIMEI36") or "").strip()
    if env:
        return env
    data_dir = ((config or {}).get("data_dir") or "data")
    p = data_dir if os.path.isabs(data_dir) else os.path.join(ROOT, data_dir)
    try:
        with open(os.path.join(p, "qimei36.txt")) as f:
            return f.read().strip()
    except Exception:
        return ""


def available(config=None, ctx=None):
    return _load() and bool(qimei36(config, ctx))


def status(config=None, ctx=None):
    ok = _load()
    q36 = qimei36(config, ctx)
    return {"ok": bool(ok and q36), "lib": LIB_PATH if ok else "",
            "qimei36": (q36[:8] + "…") if q36 else "",
            "shim_dir": SHIM_DIR,
            "ld_path_ok": _ld_path_ok(),
            "err": _STATE["err"]}


def mint(bus_info, guid="", config=None, ctx=None):
    """出一张票；失败返回 ""。"""
    if not bus_info:
        return ""
    if not _load():
        return ""
    q36 = qimei36(config, ctx)
    if not q36:
        return ""
    with _LOCK:
        try:
            _STATE["stub"].call_uk(
                ctypes.c_void_p(_STATE["fn"]), ctypes.c_void_p(_STATE["env"]),
                APP_KEY.encode(), q36.encode(), (guid or "").encode(),
                BUSID.encode(), bus_info.encode())
            raw = _STATE["stub"].get_last()
            return raw.decode() if raw else ""
        except Exception:
            return ""
