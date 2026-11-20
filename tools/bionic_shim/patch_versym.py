# 把 Android .so 的 .gnu.version 表改成 VER_NDX_GLOBAL(1)，
# 这样 glibc 加载器不再按 bionic 的 "LIBC" 版本号匹配符号。
#
# 用法: python3 patch_versym.py <原始 libqimei.so> <输出.so> [--platform bionic|glibc]
#
#   bionic（默认）——在「非 Android」环境（PC / PRoot / glibc 容器）里加载时必用。
#                    不传 -V / --version 的高版本号，加载器才会接受。
#   glibc        ——在真机 Termux（本身就是 bionic）里加载时用。
#                    保持原有的版本号不动，Android 链接器认得。
#
# 两个环境的约束是相反的，所以本脚本保留两个模式，别再改回「一律打补丁」。
import struct
import sys


def usage():
    sys.stderr.write("用法: patch_versym.py <in.so> <out.so> [--platform bionic|glibc]\n")
    sys.exit(2)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    mode = "bionic"
    for a in argv:
        if a.startswith("--platform="):
            mode = a.split("=", 1)[1]
    for i, a in enumerate(argv):
        if a == "--platform" and i + 1 < len(argv):
            mode = argv[i + 1]

    if len(args) < 2:
        usage()
    src, dst = args[0], args[1]
    if mode not in ("bionic", "glibc"):
        sys.stderr.write("--platform 只能是 bionic 或 glibc\n")
        sys.exit(2)

    d = bytearray(open(src, "rb").read())
    if d[:4] != b"\x7fELF":
        sys.stderr.write("不是 ELF 文件: %s\n" % src)
        sys.exit(2)

    is64 = d[4] == 2
    if is64:
        e_shoff = struct.unpack_from("<Q", d, 0x28)[0]
        es_off, en_off, fmt = 0x3A, 0x3C, "<IIQQQQIIQQ"
    else:
        e_shoff = struct.unpack_from("<I", d, 0x20)[0]
        es_off, en_off, fmt = 0x2E, 0x30, "<IIIIIIIIII"
    es = struct.unpack_from("<H", d, es_off)[0]
    en = struct.unpack_from("<H", d, en_off)[0]

    SHT_GNU_VERSYM = 0x6FFFFFFF
    versym = None
    for i in range(en):
        sh = struct.unpack_from(fmt, d, e_shoff + i * es)
        if sh[1] == SHT_GNU_VERSYM:
            versym = (sh[4], sh[5])   # (offset, size)
            break

    if versym is None:
        sys.stderr.write("找不到 SHT_GNU_versym 段: %s\n" % src)
        sys.exit(2)

    off, size = versym
    n = size // 2

    if mode == "glibc":
        # 真机 bionic 环境：一张表都不要动。
        open(dst, "wb").write(bytes(d))
        print("copied (glibc/bionic-native mode, versym untouched): %d entries -> %s" % (n, dst))
        return 0

    for k in range(n):
        struct.pack_into("<H", d, off + k * 2, 1)   # VER_NDX_GLOBAL
    open(dst, "wb").write(bytes(d))
    print("patched versym entries (bionic/glibc-host mode): %d -> %s" % (n, dst))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

