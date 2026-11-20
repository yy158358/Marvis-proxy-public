# -*- coding: utf-8 -*-
"""在 glibc 环境里用 Android 的 libqimei.so 出 uskey 票（手机自出票）。

前置：
  1. sh build.sh <dir> libqimei.so   # 生成垫片 + 打好版本的 libqimei_patched.so
  2. 需要环境变量 LD_LIBRARY_PATH 指向 <dir>（libandroid.so/libc.so/libm.so/libdl.so 在那）

用法：
  LD_LIBRARY_PATH=/tmp/ukshim python3 mint_uk.py <openid> [guid]

注意：调用必须带**四个**参数 (ctx, busid, bus_info, "")；只传三个会返回 "-1"。
"""
import base64
import ctypes
import sys
import uuid

LIB = "/tmp/ukshim/libqimei_patched.so"
APP_KEY = "0WIN06Y06DKV1IUQ"
APP_VER = "1.60.3400.35"
BUSID = b"marvis"


def load(path=LIB):
    lib = ctypes.CDLL(path)
    P, S = ctypes.c_void_p, ctypes.c_char_p
    init = lib.UKInstanceInit
    init.restype, init.argtypes = P, [P, S, S]
    getd = lib.UKInstanceGetDefault
    getd.restype, getd.argtypes = P, []
    get = lib.UKInstanceGetUSkey
    get.restype, get.argtypes = S, [P, S, S, S]
    return init, getd, get


def mint(bus_info, openid="", guid="", path=LIB):
    init, getd, get = load(path)
    ctx = getd()
    init(ctypes.c_void_p(ctx), APP_KEY.encode(), APP_VER.encode())
    r = get(ctypes.c_void_p(ctx), BUSID, bus_info.encode(), b"")
    return r.decode() if r else ""


def build_bus_info(openid, scene="agent_chat", model="deepseek-v4-pro-external"):
    return ("marvisAgentTag=main&marvisConversationID=conv_%s&marvisResponseID=resp_%s"
            "&marvisScene=%s&model=%s&nonce=%s&originOpenID=%s"
            % (uuid.uuid4().hex, uuid.uuid4().hex, scene, model, uuid.uuid4(), openid))


if __name__ == "__main__":
    oid = sys.argv[1] if len(sys.argv) > 1 else ""
    guid = sys.argv[2] if len(sys.argv) > 2 else ""
    bus = build_bus_info(oid)
    t = mint(bus, oid, guid)
    raw = base64.b64decode(t + "=" * (-len(t) % 4)) if t else b""
    print("bus_info =", bus)
    print("uskey     =", t)
    print("chars=%d bytes=%d magic=%s" % (len(t), len(raw), raw[:6].hex()))
