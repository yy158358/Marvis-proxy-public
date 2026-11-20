# -*- coding: utf-8 -*-
"""设备指纹头 Ual-Access-MarvisExt。

上游对话接口会依据这个下标判断请求是否来自受支持的客户端；形状对不上时
返回 403。

字段形状按官方客户端实际发出的请求对齐：

| 字段 | 取值 |
|---|---|
| `clientVersion` | agent 版本号 |
| `osVersion` | 本机操作系统版本 |
| `uskey` | base64 票据，每个请求新签 |
| `qimei36` | 36 位十六进制设备标识（设备级常量） |
| 可选字段 | `clientPlatform` / `clientPlatformVersion` / `subScene` |

`profile="client"` 时按上面这套形状构造（默认），`profile="legacy"` 保留旧形状。
生图链路固定走 legacy。

uskey 不是随机字节（见 `cap/uskey.py`）：它必须由 Beacon SDK 按
`(guid, busid, bus_info)` 现算。`uskey_mint=True` 且本机具备对应资产时走真票，
否则退回随机字节（上游会拒绝）。
"""
import base64
import json
import os
import sys
import uuid

from cap import uskey as uskey_cap

# 随机票告警只喊一次（否则每次请求都刷屏，日志会被淹掉）
_WARNED = {"random_uskey": False}

CLIENT_VERSION = "1.60.3300.765"
OS_VERSION = "Windows-10-10.0.26200"

# 官方客户端（MarvisAgent 1.60.3400.795）实测值
CLIENT_AGENT_VERSION = "1.60.3400.795"
CLIENT_OS_VERSION = "Windows-10-10.0.19045"
CLIENT_USKEY_BYTES = 393
LEGACY_USKEY_BYTES = 376

QIMEI36_CACHE = "qimei36.txt"


def _qimei36(config, ctx):
    """取真 QIMEI36：config.marvis_qimei36 > <data_dir>/qimei36.txt > 空。"""
    v = ((config or {}).get("marvis_qimei36") or "").strip()
    if v:
        return v
    base = (ctx or {}).get("data_dir") or (config or {}).get("data_dir")
    if not base:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    p = os.path.join(base, QIMEI36_CACHE)
    try:
        with open(p, encoding="utf-8") as f:
            v = f.read().strip()
    except Exception:
        return ""
    return v if len(v) == 36 else ""


def marvis_ext(guid, req_scene=1, task_type="user", profile=None,
               config=None, ctx=None, sub_scene=None,
               bus_info=None, openid="", login_type="6", nonce=None):
    """构造 Ual-Access-MarvisExt。紧凑 JSON，不能有空格（上游按原文字符串校验）。

    bus_info 给定时（对话链路），uskey 用 Beacon SDK 现算的真票；
    否则按旧形状随机（生图链路，上游不校验它）。
    """
    cfg = config or {}
    prof = (profile or cfg.get("marvis_ext_profile") or "client").lower()

    if prof == "legacy":
        return json.dumps({
            "osVersion": OS_VERSION,
            "guid": guid or "",
            "uskey": base64.b64encode(os.urandom(LEGACY_USKEY_BYTES)).decode(),
            "nonce": str(uuid.uuid4()),
            "reqScene": req_scene,
            "taskType": task_type,
            "clientVersion": CLIENT_VERSION,
            "isIoa": False,
        }, separators=(",", ":"))

    nbytes = int(cfg.get("marvis_uskey_bytes") or CLIENT_USKEY_BYTES)
    ext = {}
    q36 = _qimei36(cfg, ctx)
    if q36:
        ext["qimei36"] = q36
    nonce = str(nonce or uuid.uuid4())
    ticket = ""
    if bus_info and cfg.get("uskey_mint", True):
        ticket = uskey_cap.mint(bus_info, guid=guid or "", openid=openid,
                                login_type=login_type)
        if not ticket and not _WARNED["random_uskey"]:
            # 静默退回随机票是最贵的坑：请求一定被上游 403 4100404，
            # 却看着像"风控认了本机"。这里只喊一次，避免刷屏。
            _WARNED["random_uskey"] = True
            sys.stderr.write("[marvis_ext] 出票失败 -> 改用随机 uskey(%d 字节)，上游必然 403；"
                             "先看 cap/uskey.py 的 status() 排查通道\n" % nbytes)
            sys.stderr.flush()
    ext.update({
        "osVersion": cfg.get("marvis_os_version") or CLIENT_OS_VERSION,
        "guid": guid or "",
        "uskey": ticket or base64.b64encode(os.urandom(nbytes)).decode(),
        "nonce": nonce,
        "reqScene": req_scene,
        "taskType": task_type,
        "clientVersion": cfg.get("marvis_client_version") or CLIENT_AGENT_VERSION,
    })
    platform = cfg.get("marvis_client_platform")
    if platform:
        ext["clientPlatform"] = platform
        ext["clientPlatformVersion"] = cfg.get("marvis_client_platform_version") or CLIENT_VERSION
    if sub_scene:
        ext["subScene"] = sub_scene
    ext["isIoa"] = False
    return json.dumps(ext, separators=(",", ":"))


def wanted(config, override=None):
    """override 优先；否则看 config.send_marvis_ext（默认 True）。"""
    if override is not None:
        return bool(override)
    v = config.get("send_marvis_ext")
    return True if v is None else bool(v)
