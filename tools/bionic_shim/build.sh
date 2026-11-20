#!/bin/sh
# 在 glibc 容器/PC 上加载 bionic .so 的垫片：./build.sh <dir> <target.so>
set -e
D=${1:-.}; SO=${2:-libqimei.so}
cd "$D"
gcc -shared -fPIC -o libc.so shim.c -Wl,--version-script=ver.map -Wl,-soname,libc.so
gcc -shared -fPIC -o libm.so empty.c -Wl,--version-script=ver.map -Wl,-soname,libm.so -Wl,--no-as-needed -l:libm.so.6
gcc -shared -fPIC -o libdl.so empty.c -Wl,--version-script=ver.map -Wl,-soname,libdl.so
# libandroid.so 必须单独编译、soname 就叫 libandroid.so：libqimei_patched.so 的 DT_NEEDED
# 里写的是 `libandroid.so`，加载器按 soname 名字认领；早先 `ln -sf libc.so libandroid.so`
# 得到的副本 soname 是 libc.so，名字永远对不上，只靠 LD_LIBRARY_PATH 按文件名碰运气
#（进程启动后再改 LD_LIBRARY_PATH 无效，见 cap/uskey_local.py 的 _ensure_ld_path 注释）。
gcc -shared -fPIC -o libandroid.so shim.c -Wl,--version-script=ver.map -Wl,-soname,libandroid.so
python3 patch_versym.py "$SO" "${SO%.so}_patched.so"
echo "ok: shim 目录 $D，载入 ${SO%.so}_patched.so（不依赖 LD_LIBRARY_PATH）"
