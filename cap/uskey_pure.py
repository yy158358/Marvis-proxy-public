# -*- coding: utf-8 -*-
"""cap/uskey_pure.py —— 纯 Python 出票通道（**公开骨架，不含实现**）。

================ 这个通道是干什么的 ================

它是 `cap/uskey.py` 里 `_AUTO_ORDER` 中的 `pure` 档：
不加载任何 DLL、不依赖 node、不需要 .so，纯标准库出票。

================ 为什么公开版是空的 ================

纯 Python 能出票的前提，是知道票据的完整生成算法和其中若干个固定常量。
那部分来自对 Marvis 客户端的逆向分析，属于本项目作者的工作成果，
**不随公开仓库分发**。公开版只保留通道位置与调用契约。

================ 你能用什么 ================

| 通道 | 平台 | 需要什么 | 公开仓库可用 |
|---|---|---|---|
| `ctypes` | Windows | `beacon_wrapper.dll`（用 `tools/extract_marvis_dll.py` 提取） | ✅ 提取后可用 |
| `local` | Android / Termux | `tools/bionic_shim/*.so`（**已随仓库分发**） | ✅ 开箱可用 |
| `pure` | 任意 | 本文件的真实实现 | ❌ 需自行实现 |

所以：**手机端 clone 下来配好 shim 就能用；Windows 端提取一次 DLL 就能用。**
两者都不需要 `pure`。`pure` 的价值只是「连客户端都没有时的最后兜底」，
而在公开版里这一档留空，`cap/uskey.py` 会自动跳过它、落到其它通道。

================ 想自己实现 ================

在本文件里实现以下函数即可，`cap/uskey.py` 只依赖 `mint_ticket` 这个契约：

    mint_ticket(bus_info, guid="", openid="", login_type="6") -> str

返回 524 字符票据；失败返回 `""`（会被上层当作该通道不可用，继续降级）。
辅助函数 `build_plain` / `make_seed7` / `make_pad14` 是内部结构，可自由取舍。
"""

_PUBLIC_NOTICE = (
    "cap/uskey_pure.py 是公开骨架，纯 Python 通道未随仓库分发。\n"
    "  可用通道：\n"
    "    · Windows  → tools/extract_marvis_dll.py 提取 DLL 后走 ctypes\n"
    "    · Android  → tools/bionic_shim 已随包分发，配置 shim_dir 后走 local\n"
    "  这两个通道都不需要本文件。\n"
    "  想补全 pure 通道：在本文件实现 mint_ticket()，详见模块文档字符串"
)

__all__ = ["mint_ticket", "build_plain", "make_seed7", "make_pad14"]

# 公开骨架标记：cap/uskey.py 的 status() 会检查这个属性，
# 有它就不把 pure 报成「可用」，避免 /health 显示 pure: True 误导人。
PUBLIC_STUB = True


def _not_impl(name):
    raise NotImplementedError("%s() 在公开骨架中不可用。\n%s" % (name, _PUBLIC_NOTICE))


def make_seed7():
    """生成 7 字节 seed。"""
    _not_impl("make_seed7")


def make_pad14():
    """生成 14 字节尾部填充。"""
    _not_impl("make_pad14")


def build_plain(bus_info, rnd=None):
    """bus_info → 336 字节明文正文。"""
    _not_impl("build_plain")


def mint_ticket(bus_info, iv=None, rnd=None, seed7=None, pad14=None,
                guid="", openid="", login_type="6"):
    """出票入口。公开版直接抛 NotImplementedError，由 `cap/uskey.py` 捕获并降级。

    签名与 `cap/uskey.mint(bus_info, guid="", openid="", login_type="6")` 兼容，
    实现后可以直接接管而不用改调用方。
    """
    _not_impl("mint_ticket")
