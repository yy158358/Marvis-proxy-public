# -*- coding: utf-8 -*-
"""cap/uskey_full.py —— 纯 Python 出票引擎的**公开骨架**。

================ 这是什么 ================

Marvis 的 uskey 票据是一条 524 字符的编码串，由 10 层算法依次处理而成。
本文件在公开仓库里**只保留接口契约与流程说明，不含任何实现，也不含任何密钥常量**。

真实实现需要从腾讯 Marvis 客户端逆向得到的若干固定常量（K16 / AES_KEY16 /
TABLE_D / 自定义 base64 字母表）。这些常量不随本仓库分发。

================ 想启用怎么办 ================

三条路，按推荐顺序：

1. **`ctypes` 通道**（Windows，推荐）
   用 `tools/extract_marvis_dll.py` 从本机客户端提取 `beacon_wrapper.dll`，
   放到 `native/` 下即可。`cap/uskey.py` 会自己加载并出票，**完全不经过本文件**。

2. **`local` 通道**（Android / Termux，推荐）
   `tools/bionic_shim/*.so` 已随仓库分发，配置好 `shim_dir` 即可。
   同样**不经过本文件**。

3. **自己实现**（本文件的用途）
   如果你确实想跑纯 Python 通道，需要自行完成逆向，得到上述常量后
   在本文件里实现 `mint_ticket()`。

================ 接口契约 ================

`cap/uskey.py` 通过 `_load_pure()` 导入本模块，只要求它提供：

    mint_ticket(bus_info, guid="", openid="", login_type="6") -> str

返回 524 字符票据，失败返回 `""`。
其余函数（`build_A` / `build_s248` / `aes_cbc` 等）是内部实现细节，
公开骨架保留签名只为说明流程，**调用会抛 NotImplementedError**。
"""

_PUBLIC_NOTICE = (
    "cap/uskey_full.py 是公开骨架，不含实现。\n"
    "  · Windows：用 tools/extract_marvis_dll.py 提取 DLL 后走 ctypes 通道\n"
    "  · Android：配置 tools/bionic_shim 后走 local 通道\n"
    "  · 想跑纯 Python：需自行逆向补齐本文件，见模块文档字符串"
)


def _not_impl(name):
    raise NotImplementedError("%s() 在公开骨架中不可用。\n%s" % (name, _PUBLIC_NOTICE))


# ---------------------------------------------------------------- [10] 编码
def b64e(raw):
    """blob → 自定义字母表 base64。"""
    _not_impl("b64e")


def b64d(text):
    """自定义字母表 base64 → blob。"""
    _not_impl("b64d")


# ---------------------------------------------------------------- [2] MD5 与交织
def interleave_iv_k(iv16, k16=None):
    """IV 与 K16 交织成 32 字节。"""
    _not_impl("interleave_iv_k")


def md5_in16(iv16, k16=None):
    """交织后取 MD5 前 16 字节。"""
    _not_impl("md5_in16")


# ---------------------------------------------------------------- [3] RC4 掩码
def crc32_u32(data):
    _not_impl("crc32_u32")


def rc4_key(k16=None):
    _not_impl("rc4_key")


def rc4_prga8(key):
    _not_impl("rc4_prga8")


def build_A(in16, k16=None):
    """生成 16 字节掩码 A。"""
    _not_impl("build_A")


# ---------------------------------------------------------------- [4] s248
def rol16(v, n=8):
    _not_impl("rol16")


def build_s248(a16):
    """A → 20 字节 s248。"""
    _not_impl("build_s248")


# ---------------------------------------------------------------- [5] s610
def build_s610(s248_or_a):
    """s248 → 16 字节异或流 s610。"""
    _not_impl("build_s610")


# ---------------------------------------------------------------- [6] TEA
def tea_key():
    _not_impl("tea_key")


def tea_encrypt(v0, v1, k=None):
    _not_impl("tea_encrypt")


def tea_block8(x8, k=None):
    _not_impl("tea_block8")


def tea_decrypt(v0, v1, k=None):
    _not_impl("tea_decrypt")


def tea_dec_block8(y8, k=None):
    _not_impl("tea_dec_block8")


def pkcs7(data, block=16):
    _not_impl("pkcs7")


def seed7_from_tea(tea352):
    _not_impl("seed7_from_tea")


def tea_layer(plain338, seed7, pad14=None):
    """明文 → TEA 滑动窗口 352 字节。"""
    _not_impl("tea_layer")


# ---------------------------------------------------------------- [7][8] XOR 与 AES
def aes_cbc(data, key=None, iv=None):
    _not_impl("aes_cbc")


def aes_cbc_dec(data, key=None, iv=None):
    _not_impl("aes_cbc_dec")


def xor_layer(tea352, s610):
    _not_impl("xor_layer")


# ---------------------------------------------------------------- [9] 组装
def assemble_blob(body368, s248_20):
    """组装 392 字节 blob。"""
    _not_impl("assemble_blob")


# ---------------------------------------------------------------- 主流程
def build_ticket(plain336, iv16, seed7=None):
    _not_impl("build_ticket")


def build_ticket_from_in16(plain336, in16, seed7=None, pad14=None):
    _not_impl("build_ticket_from_in16")


def in16_from_ticket(t):
    _not_impl("in16_from_ticket")


__all__ = [
    "b64e", "b64d", "interleave_iv_k", "md5_in16", "crc32_u32",
    "rc4_key", "rc4_prga8", "build_A", "rol16", "build_s248", "build_s610",
    "tea_key", "tea_encrypt", "tea_block8", "tea_decrypt", "tea_dec_block8",
    "pkcs7", "seed7_from_tea", "tea_layer", "aes_cbc", "aes_cbc_dec",
    "xor_layer", "assemble_blob", "build_ticket", "build_ticket_from_in16",
    "in16_from_ticket",
]
