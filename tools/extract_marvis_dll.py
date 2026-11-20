# -*- coding: utf-8 -*-
"""tools/extract_marvis_dll.py —— 从本机腾讯 Marvis 客户端提取出票所需的 DLL。

================ 它做什么 ================

只做一件事：把客户端安装目录里的若干 DLL **复制**到本项目的 `native/` 下。

    复制：beacon_wrapper.dll  →  native/beacon_wrapper.dll
          qimei.dll           →  native/qimei.dll
          beacon_sdk.dll      →  native/beacon_sdk.dll

================ 它不做什么 ================

  · 不做任何逆向分析
  · 不修改、不反编译原始文件
  · 不联网、不上传任何数据

================ 为什么要这么做 ================

出票需要客户端里的 `beacon_wrapper.dll`。把它复制到本项目目录之后，
**你就可以卸载客户端了**，程序依然能出票。

================ 使用 ================

    python tools\\extract_marvis_dll.py            # 自动探测安装目录并复制
    python tools\\extract_marvis_dll.py --list     # 只列出找到的安装目录

================ 法律与条款 ================

本项目不附带腾讯的任何二进制文件。本脚本只是把**你自己机器上已安装的**
文件复制到另一个目录，供你自己的程序调用。

请在动手之前自行确认：这样做符合你所在地区的法律，以及你与腾讯之间
的服务条款。如果你不确认，请不要运行本脚本。
"""
import argparse
import os
import shutil
import sys

# 需要复制的文件
WANTED = (
    "beacon_wrapper.dll",
    "qimei.dll",
    "beacon_sdk.dll",
)

_DRIVES = "CDEFGHIJ"


def find_install_dirs():
    """列出所有 <盘>:\\...\\Tencent\\Marvis\\Application\\<ver>\\win32-native。"""
    out = []
    for d in _DRIVES:
        for sub in ("Program Files", "Program Files (x86)"):
            app = os.path.join("%s:%s" % (d, os.sep), sub)
            app = os.path.join(app, "Tencent", "Marvis", "Application")
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


def copy_files(src_dir, dst_dir):
    """复制 WANTED 里的文件。返回 (copied, missing)。"""
    os.makedirs(dst_dir, exist_ok=True)
    copied, missing = [], []
    for name in WANTED:
        src = os.path.join(src_dir, name)
        if not os.path.exists(src):
            missing.append(name)
            continue
        dst = os.path.join(dst_dir, name)
        try:
            shutil.copy2(src, dst)
            copied.append((name, os.path.getsize(dst)))
        except Exception as e:
            print("  复制 %s 失败：%r" % (name, e))
            missing.append(name)
    return copied, missing


def main():
    ap = argparse.ArgumentParser(description="从 Marvis 客户端提取出票所需 DLL")
    ap.add_argument("--list", action="store_true", help="只列出找到的安装目录")
    ap.add_argument("--src", default="", help="手工指定 win32-native 目录")
    ap.add_argument("--dst", default="", help="手工指定输出目录（默认 native/）")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst_dir = a.dst or os.path.join(root, "native")

    dirs = [a.src] if a.src else find_install_dirs()
    if not dirs:
        print("没有找到腾讯 Marvis 客户端。")
        print("已搜索：C: ~ J: 的 Program Files\\Tencent\\Marvis\\Application\\*\\win32-native")
        print()
        print("如果客户端装在非标准位置，用 --src 指定：")
        print("  python tools\\extract_marvis_dll.py --src \"D:\\path\\to\\win32-native\"")
        return 2

    print("找到 %d 个安装目录：" % len(dirs))
    for d in dirs:
        print("   ", d)
    if a.list:
        return 0

    src = dirs[0]   # 用版本最新的那个（已倒序）
    print()
    print("使用：", src)
    print("输出：", dst_dir)
    print()

    copied, missing = copy_files(src, dst_dir)
    if copied:
        print("已复制 %d 个文件：" % len(copied))
        for name, size in copied:
            print("   %-24s %10.2f MB" % (name, size / 1024 / 1024))
    if missing:
        print()
        print("缺失 %d 个（不影响主体功能）：" % len(missing))
        for name in missing:
            print("   ", name)

    if not copied:
        return 3

    print()
    print("完成。现在可以：")
    print("   run.bat           启动服务")
    print("   run.bat status    查看出票通道状态")
    print()
    print("提示：DLL 已复制到本项目目录，此后你不再需要保持客户端安装。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
