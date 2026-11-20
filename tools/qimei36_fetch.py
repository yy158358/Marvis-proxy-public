# -*- coding: utf-8 -*-
"""取本机真实 QIMEI36 并缓存到 data/qimei36.txt（给 cap/device.py 的设备指纹头用）。

QIMEI36 是设备级常量（同一台机器换 guid、换账号都不变），所以只需要取一次缓存起来。
本机没有官方客户端时取不到 —— 那就保持空，MarvisExt 里就不发这个字段。

================ 两条路，优先无依赖 ================

  ① 扫客户端本地文件（**不需要 node**）
     客户端把设备指纹写在自己的 IndexedDB / 日志里，形如
         .qimei36"$<36 位十六进制>"
     直接正则抓出来即可。只要客户端跑过就有。

  ② 调 tools/qimei36.js（需要 node）
     由 node 加载客户端自带的 qimei_napi.node 现算。
     上面那条扫不到时才走这里。

用法：
  py -3 tools\\qimei36_fetch.py           # 取一次并写缓存
  py -3 tools\\qimei36_fetch.py --show    # 只显示当前缓存值
  py -3 tools\\qimei36_fetch.py --log     # 只走本地文件扫描（不需要 node）
  py -3 tools\\qimei36_fetch.py --node    # 只走 node 通道
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

JS = os.path.join(ROOT, "tools", "qimei36.js")
CACHE = os.path.join(ROOT, "data", "qimei36.txt")

NODE_HINTS = [
    r"%LOCALAPPDATA%\Programs\nodejs\node.exe",
    r"%ProgramFiles%\nodejs\node.exe",
]


def find_node():
    env = os.environ.get("QIMEI_NODE")
    if env and os.path.exists(env):
        return env
    p = shutil.which("node") or shutil.which("node.exe")
    if p:
        return p
    for h in NODE_HINTS:
        h = os.path.expandvars(h)
        if os.path.exists(h):
            return h
    for pat in (r"C:\Program Files\nodejs\node.exe",
                "/usr/bin/node",
                "/usr/local/bin/node"):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def fetch_from_logs():
    """① 扫客户端本地文件。不需要 node。返回 36 位字符串或 ""。"""
    try:
        from cap import autoconfig
    except Exception as e:
        print("无法加载 cap.autoconfig: %r" % (e,), file=sys.stderr)
        return ""
    return autoconfig.detect_qimei36_from_logs()


def fetch_from_node(app_version, guid):
    """② 调 node 现算。返回 36 位字符串或 ""。"""
    node = find_node()
    if not node:
        print("找不到 node（用环境变量 QIMEI_NODE 指定 node.exe）", file=sys.stderr)
        return ""
    if not os.path.exists(JS):
        print("找不到 %s" % JS, file=sys.stderr)
        return ""
    cmd = [node, JS, "--app-version", app_version]
    if guid:
        cmd += ["--guid", guid]
    print("运行：" + " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90)
    except Exception as e:
        print("调用 node 失败：%r" % (e,), file=sys.stderr)
        return ""
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        print("取 QIMEI36 失败 rc=%s" % r.returncode, file=sys.stderr)
        print((r.stderr or "").strip()[:600], file=sys.stderr)
        return ""
    try:
        info = json.loads(out.splitlines()[-1])
    except Exception:
        print("输出无法解析：" + out[:400], file=sys.stderr)
        return ""
    q36 = (info.get("qimei36") or "").strip()
    if len(q36) != 36:
        print("QIMEI36 长度异常：%r" % q36, file=sys.stderr)
        return ""
    return q36


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只显示当前缓存值")
    ap.add_argument("--log", action="store_true", help="只走本地文件扫描")
    ap.add_argument("--node", action="store_true", help="只走 node 通道")
    ap.add_argument("--app-version", default="", help="客户端版本号（node 通道用）")
    ap.add_argument("--guid", default="", help="指定 guid（实测与 Q36 无关）")
    a = ap.parse_args()

    if a.show:
        v = ""
        if os.path.exists(CACHE):
            with open(CACHE, encoding="utf-8") as f:
                v = f.read().strip()
        print("缓存 %s：%s" % (CACHE, v or "(空)"))
        return 0 if v else 1

    q36 = ""

    # ① 无依赖路径
    if not a.node:
        q36 = fetch_from_logs()
        if q36:
            print("从客户端本地文件取到：%s" % q36)

    # ② node 兜底
    if not q36 and not a.log:
        ver = a.app_version
        if not ver:
            try:
                from cap import autoconfig
                dirs = autoconfig._marvis_app_dirs()
                if dirs:
                    ver = os.path.basename(os.path.dirname(dirs[0]))
            except Exception:
                pass
        q36 = fetch_from_node(ver or "1.60.3400.35", a.guid)

    if not q36:
        print()
        print("没能取到 QIMEI36。可选做法：", file=sys.stderr)
        print("  1) 确认本机装过 Marvis 客户端并至少运行过一次", file=sys.stderr)
        print("  2) 手动在客户端数据里搜 qimei36，把 36 位值填进 config.json 的"
              " marvis_qimei36", file=sys.stderr)
        print("  3) 装个 Node.js 后重跑本脚本", file=sys.stderr)
        return 3

    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        f.write(q36 + "\n")
    print("QIMEI36 = %s" % q36)
    print("已缓存到 %s（cap/device.py 会自动读取）" % CACHE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
