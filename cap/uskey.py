# -*- coding: utf-8 -*-
"""uskey 票据（Ual-Access-MarvisExt.uskey）的产出。

## 这是什么

上游 `POST /v1/chat/completions` 校验请求头里的 `uskey`。它是一段由
Beacon SDK 依据**设备侧材料**与**本次请求上下文**算出的不透明票据
（base64 后约 524 字符），每次请求都要重新生成。

## 依赖（四条通道，按顺序尝试）

1. **ctypes 通道（首选，不需要 Node）**：`ctypes.WinDLL` 加载 `beacon_wrapper.dll`。
2. **pure 通道**：`cap/uskey_pure.py`（原语在 `cap/uskey_full.py`）纯 Python 实现，
   不加载 DLL、不需要 Node。公开版这里是接口骨架，不含实现。
3. **node 通道**：`beacon_napi.node` + 一个 Node.js。
4. **local 通道**：Android 的本机 `.so`（`cap/uskey_local.py` + `tools/bionic_shim/`）。
   **只在 POSIX 上可用**，Windows 上 `available()` 恒 False。

四条通道各自有平台前提，缺资产的那条只会返回 ""，不抛异常——所以 PC 与手机
可以共用同一份 `cap/uskey.py`。四条全不可用时调用方退回空串（上游会拒绝）。

## 用法

    from cap import uskey
    uskey.available()                        # 是否可用
    uskey.mint(bus_info, guid, openid, "6")  # 出票，失败返回 ""
"""
import ctypes
import json
import os
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "tools", "uskey_mint.js")
APP_KEY = "0WIN06Y06DKV1IUQ"
APP_VER = "1.60.3400.35"      # 兜底值；实际优先用安装目录名推断，见 _detect_app_ver
BUSID = "marvis"

# 从 DLL 内部取出 Client 单例的入口偏移（DLL 相对虚拟地址）。
#
# 注意：客户端每升级一次这个偏移都会变，写死会让进程直接崩
# （旧值在新版里落在别的代码中间，调用即 STATUS_STACK_BUFFER_OVERRUN）。
#
# 三级取值顺序（越靠前优先级越高）：
#   1. 环境变量 MARVIS_CTX_GETTER_RVA（支持 0x 前缀或十进制）——现场急救用
#   2. config.json 的 uskey_ctx_getter_rva——长期配置用
#   3. 下面的 _DEFAULT_CTX_GETTER_RVA——出厂兜底
# 运行时 _load_ctypes 仍会先用 _detect_ctx_getter 自动探测，探测失败才用上面这个值。
_DEFAULT_CTX_GETTER_RVA = 0x3E330
_CTX_GETTER_RVA = _DEFAULT_CTX_GETTER_RVA
_APP_VER_EFF = ""       # 实际传给 beacon_init 的版本号（探测失败时为空）
_CTX_GETTER_EFF = 0     # 实际使用的 getter 偏移（探测失败时为 0）
_OUT_LEN = 4096
_CTX = None
_DLL = None
_CTYPES_TRIED = False
_CTYPES_OK = False
_CTYPES_ERR = ""

_NODE_CANDIDATES = (
    os.environ.get("MARVIS_NODE", ""),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "node.exe"),
    r"C:\Program Files\nodejs\node.exe",
)

_NODE = None
_NATIVE = None
_PROBED = False
_LOCK = threading.Lock()
_PROC = None
_CACHE = {}
_CACHE_MAX = 64

# ── 出票后端选择 ─────────────────────────────────────────────────────────────
# auto   : ctypes → pure → node → local，任一成功即用（默认）
# ctypes : 只用 DLL（Windows 客户端 beacon_wrapper.dll）
# pure   : 只用纯 Python（cap.uskey_pure，不加载 DLL）
# node   : 只用 node worker（beacon_napi.node）
# local  : 只用本机 .so（Android 版 libqimei，见 cap/uskey_local.py）
# 可用环境变量 MARVIS_USKEY_BACKEND 覆盖，也可用 config.json 的 uskey_backend 覆盖。
#
# 通道各自有平台前提，缺资产时只会返回 ""，不会抛异常：
#   ctypes / node 要 Windows 客户端目录；pure 要 cap/uskey_pure.py；
#   local 要 POSIX + tools/bionic_shim 下的 .so。所以两端同一份代码可以共存。
BACKENDS = ("auto", "ctypes", "pure", "node", "local")
_BACKEND = "auto"
_FORCED = ""            # set_backend() 强设值，优先级最高
_PURE = None            # cap.uskey_pure，懒加载
_PURE_ERR = ""
_LOCAL = None           # cap.uskey_local，懒加载
_LOCAL_ERR = ""

# 各后端最近一次成败，给 /health 看
_LAST = {"ctypes": None, "pure": None, "node": None, "local": None}

# auto 模式的通道顺序（mint() 与 status() 共用这一份，别再各写一遍）。
#
# 排序依据是「有资产就先用，全没有才用兜底」，不是单纯比速度：
#   1. ctypes  Windows 专属，需 beacon_wrapper.dll。进程内 ctypes 调用，实测 0.33 ms/票，最快。
#   2. local   手机专属，需 tools/bionic_shim/*.so。同样是进程内 ctypes，快；
#              且它是手机上唯一能出真票的通道（其它三个在 POSIX 上都不可用）。
#   3. pure    零依赖，纯 Python。实测 3.55 ms/票，比 ctypes 慢约 10 倍，
#              但谁都跑得起来 —— 这是「clone 下来什么都不装也能用」的保底通道，
#              所以排在所有需要特定资产的通道之后。
#   4. node    需要 beacon_napi.node + 匹配 ABI 的 node.exe，还要维持常驻子进程，
#              通信走 stdin/stdout 行 JSON，任何一次解析失败就杀进程重启。代价最高，排最后。
#              它和 local 永远不会同时可用（一个 Windows 一个 POSIX），排后面只影响语义。
_AUTO_ORDER = ("ctypes", "local", "pure", "node")


def _find_node():
    for c in _NODE_CANDIDATES:
        if c and os.path.exists(c):
            return c
    # DSH 运行时里的 node（版本较新，能直接 require 这个 ABI 的插件）
    base = os.path.join(os.environ.get("USERPROFILE", ""), ".dsh", "dsh-runtimes")
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name, "dependencies", "node", "bin", "node.exe")
            if os.path.exists(p):
                return p
    return None


def _find_native():
    """找 beacon_napi.node / beacon_wrapper.dll 所在目录（客户端可能不在 C 盘）。"""
    env = os.environ.get("MARVIS_NATIVE_DIR", "")
    if env and (os.path.exists(os.path.join(env, "beacon_napi.node"))
                or os.path.exists(os.path.join(env, "beacon_wrapper.dll"))):
        return env
    drives = [os.environ.get("SystemDrive", "C:")] + \
             ["%s:" % c for c in "CDEFGH"]
    seen = []
    for d in drives:
        if not d or d in seen:
            continue
        seen.append(d)
        base = os.path.join(d + os.sep, "Program Files", "Tencent", "Marvis", "Application")
        if not os.path.isdir(base):
            continue
        for ver in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, ver, "win32-native")
            if os.path.exists(os.path.join(p, "beacon_napi.node")):
                return p
    return None


def _parse_rva(v):
    """把 '0x3E330' / '254512' / 254512 统一转成 int，失败返回 0。"""
    if isinstance(v, int):
        return v if v > 0 else 0
    if not v:
        return 0
    try:
        s = str(v).strip()
        n = int(s, 16) if s.lower().startswith("0x") else int(s, 10)
        return n if n > 0 else 0
    except Exception:
        return 0


def _resolve_ctx_getter_rva():
    """按 环境变量 > config.json > 出厂常量 的顺序取 ctx getter 偏移。

    环境变量和配置都拿不到，就用 _DEFAULT_CTX_GETTER_RVA。
    结果写回模块级 _CTX_GETTER_RVA，供 _load_ctypes 兜底使用。
    """
    global _CTX_GETTER_RVA
    n = _parse_rva(os.environ.get("MARVIS_CTX_GETTER_RVA", ""))
    if n:
        _CTX_GETTER_RVA = n
        return n
    try:
        cfg = os.path.join(ROOT, "config.json")
        if os.path.exists(cfg):
            import json
            with open(cfg, "r", encoding="utf-8") as f:
                n = _parse_rva(json.load(f).get("uskey_ctx_getter_rva"))
            if n:
                _CTX_GETTER_RVA = n
                return n
    except Exception:
        pass
    _CTX_GETTER_RVA = _DEFAULT_CTX_GETTER_RVA
    return _CTX_GETTER_RVA


def _detect_app_ver(native_dir):
    """从 <盘>:\\...\\Marvis\\Application\\<ver>\\win32-native 推断客户端版本号。

    升级后目录名就是新版本号，比硬编码的 APP_VER 可靠得多
    （1.60.3400.35 → 1.60.3500.48 这种变化，写死就得每次改代码）。
    推断不出来就返回空串，调用方退回 APP_VER。
    """
    try:
        ver = os.path.basename(os.path.dirname(os.path.normpath(native_dir or "")))
    except Exception:
        return ""
    if ver and ver[0].isdigit() and all(c.isdigit() or c == "." for c in ver):
        return ver
    return ""


def _rva_to_off(data, rva):
    """PE 的 RVA 转文件偏移（只解析节表，不加载 DLL）。"""
    try:
        import struct
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        optsz = struct.unpack_from("<H", data, pe + 20)[0]
        sb = pe + 24 + optsz
        for i in range(nsec):
            o = sb + i * 40
            vsz, va, rsz, raw = struct.unpack_from("<IIII", data, o + 8)
            if va <= rva < va + max(vsz, rsz):
                return raw + (rva - va)
    except Exception:
        return None
    return None


def _text_section(data):
    """返回 (.text 的 RVA, 长度)，失败返回 None。用于校验 call 目标是否落在代码段。"""
    try:
        import struct
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        optsz = struct.unpack_from("<H", data, pe + 20)[0]
        sb = pe + 24 + optsz
        for i in range(nsec):
            o = sb + i * 40
            name = data[o:o + 8].rstrip(b"\x00")
            vsz, va, rsz, raw = struct.unpack_from("<IIII", data, o + 8)
            if name in (b".text", b"CODE"):
                return (va, max(vsz, rsz))
    except Exception:
        return None
    return None


def _detect_ctx_getter(native_dir, dll_handle):
    """自动探测取 Client 单例的入口偏移。

    做法：定位 `beacon_get_uskey` 的函数体，取其中第一个合法的 `call` 目标。
    这是不依赖具体版本号的通用特征——客户端升级后不必手工改常量。

    实现分两档，都不要求装 capstone：
      A. 有 capstone 就用它逐条译码（准，能跳过多字节指令）；
      B. 没 capstone 就手工扫 `E8 rel32`（call 立即数）字节模式，再用下面几条规则筛：
         - 目标必须落在代码节内；
         - 目标 != beacon_get_uskey 自身（防自递归）；
         - 越界忽略。
    只取第一个合法的 call 目标。

    探测失败返回 0，调用方退回 _CTX_GETTER_RVA。
    """
    fp = os.path.join(native_dir or "", "beacon_wrapper.dll")
    try:
        with open(fp, "rb") as f:
            data = f.read()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetProcAddress.restype = ctypes.c_void_p
        k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        addr = k32.GetProcAddress(ctypes.c_void_p(dll_handle), b"beacon_get_uskey")
        if not addr:
            return 0
        self_rva = addr - dll_handle
        off = _rva_to_off(data, self_rva)
        if off is None:
            return 0
        text = _text_section(data)
        lo, hi = (text[0], text[0] + text[1]) if text else (0, len(data))

        # 档 A：capstone（可选）
        try:
            import capstone
        except Exception:
            capstone = None
        if capstone is not None:
            try:
                md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
                md.skipdata = True
                md.detail = True
                n = 0
                for ins in md.disasm(data[off:off + 260], self_rva):
                    if ins.mnemonic == "call" and ins.operands \
                            and ins.operands[0].type == capstone.x86.X86_OP_IMM:
                        tgt = ins.operands[0].imm
                        if lo <= tgt < hi and tgt != self_rva:
                            return tgt
                    n += 1
                    if n >= 40:
                        break
                return 0
            except Exception:
                pass

        # 档 B：手扫 E8 rel32
        import struct
        end = min(off + 260, len(data) - 5)
        i = off
        while i < end:
            if data[i] == 0xE8:
                rel = struct.unpack_from("<i", data, i + 1)[0]
                tgt = (self_rva + (i - off) + 5 + rel) & 0xFFFFFFFFFFFFFFFF
                if lo <= tgt < hi and tgt != self_rva:
                    return tgt
            i += 1
    except Exception:
        return 0
    return 0


def _load_ctypes():
    """纯 Python 通道：直接 ctypes 调 beacon_wrapper.dll。

    返回 True 表示通道可用（dll 加载成功 + init 成功 + 单例 ctx 已拿到）。
    Windows 以外直接返回 False（Android 加载不了这个 PE）。
    """
    global _DLL, _CTX, _CTYPES_TRIED, _CTYPES_OK, _CTYPES_ERR, _NATIVE
    global _APP_VER_EFF, _CTX_GETTER_EFF
    if _CTYPES_TRIED:
        return _CTYPES_OK
    _CTYPES_TRIED = True
    if os.name != "nt":
        _CTYPES_ERR = "not windows"
        return False
    if not _NATIVE:
        # 允许直接调本函数（不必先走 available()），自己补一次探测
        _NATIVE = _find_native()
    if not _NATIVE:
        _CTYPES_ERR = "win32-native not found"
        return False
    fp = os.path.join(_NATIVE, "beacon_wrapper.dll")
    if not os.path.exists(fp):
        _CTYPES_ERR = "beacon_wrapper.dll not found in %s" % _NATIVE
        return False
    try:
        fb = _NATIVE.encode("utf-8", "replace")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(fb)
        dll = ctypes.WinDLL(fp)
        dll.beacon_init.restype = ctypes.c_int
        dll.beacon_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        dll.beacon_set_common_params.restype = ctypes.c_int
        dll.beacon_set_common_params.argtypes = [ctypes.c_char_p]
        dll.beacon_get_uskey.restype = ctypes.c_int
        dll.beacon_get_uskey.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_int]
        # beacon_init 的第 2 个参数是客户端版本号，服务端会核对。优先用安装目录名推断。
        app_ver = _detect_app_ver(_NATIVE) or APP_VER
        _APP_VER_EFF = app_ver
        rc = dll.beacon_init(APP_KEY.encode("ascii"), (app_ver or "").encode("ascii"))
        if rc != 0:
            _CTYPES_ERR = "beacon_init rc=%d" % rc
            return False
        base = ctypes.cast(dll._handle, ctypes.c_void_p).value
        # 单例 getter 偏移每次升级都会变，先自动探测；探测不到才用配置值兜底。
        fallback = _resolve_ctx_getter_rva()
        _CTX_GETTER_EFF = _detect_ctx_getter(_NATIVE, base) or fallback
        getter = ctypes.CFUNCTYPE(ctypes.c_void_p)(base + _CTX_GETTER_EFF)
        ctx = getter()
        if not ctx:
            _CTYPES_ERR = "ctx=null"
            return False
        _DLL = dll
        _CTX = ctx
        _CTYPES_OK = True
        return True
    except Exception as e:
        _CTYPES_ERR = repr(e)
        _DLL = None
        _CTX = None
        _CTYPES_OK = False
        return False


def _mint_ctypes(bus_info):
    """走 ctypes 出票。失败返回 ""。"""
    if not _DLL or not _CTX:
        return ""
    try:
        ib = ctypes.create_string_buffer(bus_info.encode("utf-8"), len(bus_info) + 1)
        ob = ctypes.create_string_buffer(_OUT_LEN)
        rc = _DLL.beacon_get_uskey(_CTX, ctypes.cast(ib, ctypes.c_void_p),
                                   ctypes.cast(ob, ctypes.c_void_p), _OUT_LEN)
        if rc <= 0:
            return ""
        raw = bytes(ob.raw)[:rc]
        return raw.split(b"\x00")[0].decode("ascii", "replace")
    except Exception:
        return ""


def available():
    """任一出票通道可用 → True。第一次调用会做探测并记住结果。"""
    global _NODE, _NATIVE, _PROBED
    if not _PROBED:
        _NATIVE = _find_native()
        _PROBED = True
        if _load_ctypes():
            return True
        _NODE = _find_node()
    return bool(_CTX and _DLL) or bool(_NODE and _NATIVE and os.path.exists(WORKER))


def status():
    """出票后端状态。

    backend  = 实际会用的通道（按 selected 与各通道可用性算出来的，不是简写）
    selected = 配置值（auto / ctypes / pure / node / local）
    detail   = 各通道的探测结果，排障时看这里

    local 是手机端专属的本机 .so 通道（cap/uskey_local.py）。Windows 上它恒为 False，
    但不影响其它通道，所以两端用同一份代码。
    """
    available()
    sel = backend()
    has_ct = bool(_CTX and _DLL)
    has_pu = _load_pure() is not None
    has_nd = bool(_NODE and _NATIVE and os.path.exists(WORKER))
    has_lo = _local_ready()
    avail = {"ctypes": has_ct, "pure": has_pu, "node": has_nd, "local": has_lo}
    if sel == "auto":
        eff = ""
        for name in _AUTO_ORDER:
            if avail[name]:
                eff = name
                break
    else:
        eff = sel if avail.get(sel) else ""
    return {"ok": has_ct or has_pu or has_nd or has_lo,
            "backend": eff,
            "selected": sel,
            "node": _NODE or "", "native": _NATIVE or "",
            # 这两个是升级后最容易失效的东西，暴露出来便于一眼诊断
            "app_ver": _APP_VER_EFF or APP_VER,
            "ctx_getter": ("0x%X" % _CTX_GETTER_EFF) if _CTX_GETTER_EFF else "",
            "detail": {"ctypes": has_ct, "pure": has_pu, "node": has_nd, "local": has_lo},
            "pure_err": _PURE_ERR,
            "local_err": _LOCAL_ERR,
            "last": {k: v for k, v in _LAST.items() if v is not None},
            "worker": WORKER if os.path.exists(WORKER) else ""}


def _worker():
    """常驻 node worker（进程内单例）。挂了下次自动重启。"""
    global _PROC
    if _PROC is not None and _PROC.poll() is None:
        return _PROC
    if not available():
        return None
    payload = json.dumps({"native": _NATIVE, "appkey": APP_KEY, "appver": APP_VER})
    try:
        _PROC = subprocess.Popen(
            [_NODE, WORKER, payload],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1)
    except Exception:
        _PROC = None
    return _PROC


def backend():
    """返回当前生效的出票后端。优先级：set_backend 强设 > 环境变量 > config.json > auto。"""
    global _BACKEND
    if _FORCED in BACKENDS:
        return _FORCED
    env = (os.environ.get("MARVIS_USKEY_BACKEND") or "").strip().lower()
    if env in BACKENDS:
        _BACKEND = env
        return env
    try:
        cfg_path = os.path.join(ROOT, "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                v = (json.load(f).get("uskey_backend") or "").strip().lower()
            if v in BACKENDS:
                _BACKEND = v
                return v
    except Exception:
        pass
    return _BACKEND if _BACKEND in BACKENDS else "auto"


def set_backend(name):
    """强行切换后端（测试/诊断用），优先级高于环境变量与 config.json。

    传 None 取消强设，恢复按配置走。
    """
    global _BACKEND, _FORCED
    if name is None:
        _FORCED = ""
        return backend()
    if name not in BACKENDS:
        raise ValueError("未知后端 %r，可选：%s" % (name, ", ".join(BACKENDS)))
    _FORCED = name
    _BACKEND = name
    return _BACKEND


def _note(name, ok):
    """记录某通道最近一次成败，供 /health 展示。"""
    _LAST[name] = bool(ok)


def _load_pure():
    """懒加载纯 Python 出票模块。失败返回 None 并记下原因。

    公开版本里 cap/uskey_pure.py 是骨架（带 PUBLIC_STUB = True），能导入但没实现。
    这种情况也按「不可用」处理，否则 status() 会误报 pure: True。
    """
    global _PURE, _PURE_ERR
    if _PURE is not None:
        return _PURE
    if _PURE_ERR:
        return None
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from cap import uskey_pure as _up
        if getattr(_up, "PUBLIC_STUB", False):
            _PURE_ERR = "公开骨架（未实现），pure 通道不可用"
            return None
        _PURE = _up
        return _PURE
    except Exception as e:
        _PURE_ERR = repr(e)
        return None


def _mint_pure(bus_info):
    """纯 Python 通道：不加载任何 DLL，也不需要 node。失败返回 ""。"""
    mod = _load_pure()
    if mod is None:
        _note("pure", False)
        return ""
    try:
        t = mod.mint_ticket(bus_info)
        _note("pure", bool(t))
        return t or ""
    except Exception:
        _note("pure", False)
        return ""


def _load_local():
    """懒加载本机 .so 出票通道（Android 版 libqimei，见 cap/uskey_local.py）。

    这条通道是手机端专属：需要 POSIX + tools/bionic_shim 下那套 .so。
    在 Windows 上 import 会成功、available() 返回 False，
    所以两端共用同一份 cap/uskey.py 也不会互相干扰。
    """
    global _LOCAL, _LOCAL_ERR
    if _LOCAL is not None:
        return _LOCAL
    if _LOCAL_ERR:
        return None
    try:
        from cap import uskey_local as _ul
        _LOCAL = _ul
        return _LOCAL
    except Exception as e:
        _LOCAL_ERR = repr(e)
        return None


def _local_ready():
    """本机 .so 通道当前是否真能出票（资产齐 + qimei36 有值）。"""
    mod = _load_local()
    if mod is None:
        return False
    try:
        return bool(mod.available())
    except Exception:
        return False


def _mint_local(bus_info, guid=""):
    """本机 .so 通道：Android 上用 libqimei 出票（332 字符）。失败返回 ""。"""
    mod = _load_local()
    if mod is None:
        _note("local", False)
        return ""
    try:
        t = mod.mint(bus_info, guid=guid or "")
        _note("local", bool(t))
        return t or ""
    except Exception:
        _note("local", False)
        return ""


def mint(bus_info, guid="", openid="", login_type="6", timeout=10):
    """按 bus_info 出一张票。失败返回 ""（调用方退回随机 uskey）。

    通道优先级见模块级 _AUTO_ORDER（backend=auto 时按它依次试）：
        ctypes（DLL）→ local（本机 .so）→ pure（纯 Python）→ node
    前两条是「有资产就更快」的通道，pure 是零依赖兜底，node 代价最高排最后。
    两端各自缺的通道会自动跳过，所以同一份代码在 PC 与手机上都能跑。

    注意：出票依赖 bus_info，与 guid / openid / login_type 无关（实测），
    这三个参数保留只为兼容旧调用方。
    """
    if not bus_info:
        return ""
    key = (bus_info, guid, openid)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        u = ""
        bk = backend()
        for name in (_AUTO_ORDER if bk == "auto" else (bk,)):
            if name == "ctypes":
                if available() and _load_ctypes():
                    u = _mint_ctypes(bus_info)
                _note("ctypes", bool(u))
            elif name == "local":
                u = _mint_local(bus_info, guid)
            elif name == "pure":
                u = _mint_pure(bus_info)
            elif name == "node":
                u = _mint_node(bus_info, guid, openid, login_type)
                _note("node", bool(u))
            if u:
                break
        if not u:
            return ""
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = u
        return u


def _mint_node(bus_info, guid, openid, login_type):
    """兜底通道：常驻 node 子进程。失败返回 ""。"""
    proc = _worker()
    if proc is None:
        return ""
    req = {"busid": BUSID, "businfo": bus_info,
           "common": {"guid": guid or "", "login_openid": openid or "",
                      "login_type": str(login_type or "6")}}
    try:
        proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        proc.stdin.flush()
    except Exception:
        _kill()
        return ""
    try:
        line = proc.stdout.readline()
    except Exception:
        _kill()
        return ""
    try:
        obj = json.loads(line or "{}")
    except Exception:
        _kill()
        return ""
    return obj.get("uskey") or ""


def _kill():
    global _PROC
    try:
        if _PROC is not None:
            _PROC.kill()
    except Exception:
        pass
    _PROC = None


def reset_cache():
    with _LOCK:
        _CACHE.clear()


def bus_info(tag, conv, resp, scene, model, nonce, openid):
    """按客户端真实格式拼 bus_info（顺序固定，不要改）。"""
    return "&".join([
        "marvisAgentTag=%s" % (tag or "main"),
        "marvisConversationID=%s" % (conv or ""),
        "marvisResponseID=%s" % (resp or ""),
        "marvisScene=%s" % (scene or "agent_chat"),
        "model=%s" % (model or "main-auto"),
        "nonce=%s" % (nonce or ""),
        "originOpenID=%s" % (openid or ""),
    ])
