# 马维斯反代（marvis-proxy）

把马维斯 App 账号里的对话模型和文生图，包装成本机可用的 **OpenAI / Anthropic 兼容接口**。
Python 标准库实现，不需要 pip 装任何包，默认只听 `127.0.0.1`。

**同一份代码同时支持 PC 与手机（Android / Termux / PRoot）**。

打开 <http://127.0.0.1:8088/> 就是 Web 控制台：账号池、用量、模型、接线、密钥、路由图都在里面，
命令行也全部能用

---

## 首次使用：点这三个文件

### Windows

| 顺序 | 文件 | 作用 |
|---|---|---|
| 1 | `tools\extract_marvis_dll.py` | 从已装的客户端里提取出票用的 DLL 到 `native\`。**只需一次** |
| 2 | `run.bat` | 启动服务（前台，`Ctrl+C` 停） |
| 3 | `run.bat status` | 看出票通道和账号池是否正常 |

```bat
python tools\extract_marvis_dll.py
run.bat
```

### Android / Termux

| 顺序 | 文件 | 作用 |
|---|---|---|
| 1 | `run.sh` | 启动服务。出票用的 `.so` 已随仓库放在 `tools/bionic_shim/` |

```sh
sh run.sh start
```

**也可以完全不用仓库里的 `.so`**：如果你已经在这台手机上装了马维斯 App，可以直接从
App 里取出它自带的 `libqimei.so` 来用，效果一样，而且不需要 root：

```sh
# 1) 找到 App 的安装位置并拷出 .so（不需要 root）
PKG=com.tencent.android.marvis
APK=$(pm path "$PKG" | sed 's/^package://')
cp "$APK" /data/local/tmp/m.apk

# 2) 解出 arm64 的 libqimei.so
mkdir -p /data/local/tmp/q && cd /data/local/tmp/q
unzip -o /data/local/tmp/m.apk 'lib/arm64-v8a/*' >/dev/null

# 3) 放进一个落在真实文件系统上的目录（不要放 /sdcard）
mkdir -p ~/.marvis-shim
cp lib/arm64-v8a/libqimei.so ~/.marvis-shim/libqimei_patched.so
```

> 文件名**必须**是 `libqimei_patched.so`——代码按这个名字去找（`cap/uskey_local.py`）。
> 在真机 Termux 里它其实没被「打补丁」，只是沿用这个文件名；名字对不上就只会
> 一直报「缺少 libqimei_patched.so 或 libjni_stub.so」。

然后指定目录启动：

```sh
MARVIS_SHIM_DIR=~/.marvis-shim sh run.sh start
```

> **在真机 Termux 里**，`libqimei.so` 中间不需要打补丁——Termux 用的就是 bionic，
> 和 App 自己的运行环境一致。仓库里那个 `libqimei_patched.so` 是给 **PRoot / 桌面 Linux**
> 这类 **glibc** 环境用的（`patch_versym.py` 把符号版本号抹平，glibc 加载器才肯接受）。
>
> 要在 glibc 环境（PRoot）里用你自己提取的那一份，就生成一份打过补丁的：
>
> ```sh
> python3 tools/bionic_shim/patch_versym.py \
>     lib/arm64-v8a/libqimei.so \
>     ~/.marvis-shim/libqimei_patched.so --platform bionic
> ```
>
> 其余四个垫片（`libc.so` / `libm.so` / `libdl.so` / `libandroid.so`）由
> `tools/bionic_shim/build.sh` 用 gcc 现场编出来，内容是空的，不是腾讯的二进制。

---

## 命令行

### `run.bat`（Windows）

| 命令 | 作用 |
|---|---|
| `run.bat` | 前台启动（默认 `127.0.0.1:8088`，`Ctrl+C` 停） |
| `run.bat bg` | 后台启动，日志追加到 `logs\serve.log` |
| `run.bat status` | 看状态 + `/health` |
| `run.bat log 100` | 看最近 100 行日志 |
| `run.bat stop` | 停止 |
| `run.bat accept fast` | 验收：key 覆盖 + 模型列表（约 5 秒） |
| `run.bat accept` | 验收全量：再加 2 个模型与一张生图 |
| `backup.bat` | 立即备份到 `backups\` |

解释器优先找 `py -3`，找不到回退 `python`（要求 ≥ 3.8）。

### `run.sh`（POSIX）

参数与 `run.bat` 一一对应：

```sh
sh run.sh start|bg|stop|status|log|shim|autostart
```

### 工具

| 命令 | 作用 |
|---|---|
| `python tools\selfcheck.py` | 自检，输出 `PASS / WARN / FAIL` 计数（需先备好 key 和数据，见下） |
| `python tools\extract_marvis_dll.py --list` | 只列出找到的客户端安装目录，不复制 |
| `python tools\qimei36_fetch.py` | 手动取一次设备指纹并缓存 |
| `python tools\qimei36_fetch.py --log` | 只扫本机客户端数据（不需要 Node.js） |
| `python tools\admin.py help` | 管理命令行（加号、API Key、账号池），见下 |
| `python tools\task_tail.py` | 看最近生图任务（`--limit 50 --state failed` 筛失败） |
| `python tools\wire_selftest.py` | 接线的离线自测：4 个 agent × 各形态配置 → apply → rollback → 逐字节比对 |

Windows 上跑自检如果报 `UnicodeEncodeError`，先 `set PYTHONIOENCODING=utf-8`。

**自检前先把 key 放进 `data\client_keys.txt`**，否则 B/C 段所有带 key 的用例都会报 401：

```bat
:: Windows
mkdir data 2>nul
echo mvk_你的key> data\client_keys.txt
python tools\selfcheck.py
```

```sh
# Android / Termux
mkdir -p data
echo "mvk_你的key" > data/client_keys.txt
python tools/selfcheck.py
```

（`data/` 不入版本管理，所以自检也要等 `run` 至少启动过一次、`data\marvis.db` 已经建好再看，否则 D 段会显示「跳过」。）

---

## 加号

启动后必须先加号，否则对话请求回 `503 no_account`。**加号就是把你客户端的登录态
导进来**，本项目不提供任何绕过登录的办法。

```bat
python tools\admin.py login start
```

会打印一个授权链接，例如：

```
====================================================================
  在手机浏览器里打开这个链接，登录并授权：

  https://graph.qq.com/oauth2.0/authorize?response_type=code&client_id=...

  state : 50f752f6
  注意  : 本链接约 5 分钟有效，别重复点 start（会换 state）
====================================================================
```

**流程**：

| 步骤 | 做什么 |
|---|---|
| 1 | 手机浏览器打开上面那个链接 |
| 2 | 登录你的账号 → 授权 |
| 3 | 授权后页面会跳转，**复制落地页的整段内容**（JSON 正文优先，没有就复制地址栏整条 URL） |
| 4 | 回到命令行执行 `login exchange` 把这段内容贴回去 |

```bat
python tools\admin.py login exchange --text "<刚复制的整段内容>"
```

成功会打印 `加号成功：id=1 openid=ABC8CD… token_len=… refresh=True`，后面跟一个探测结果。

微信登录加 `--type WX`：

```bat
python tools\admin.py login start --type WX
```

> 微信链接同机浏览器通常会要求「用微信打开」，所以要把链接发到另一台设备扫码。

### 账号管理

```bat
python tools\admin.py accounts          :: 看账号池（id / 状态 / 成功失败数 / 并发）
python tools\admin.py account 3 probe   :: 探测某个账号还能不能用
python tools\admin.py account 3 refresh :: 手动续期
python tools\admin.py account 3 disable :: 停用（enable 恢复，delete 删除）
```

---

## 创建 API Key

服务默认**只监听 `127.0.0.1`，不校验 key**（本机谁都能用）。一旦你创建了第一个启用的
key，`/v1/*` 就变成必须带 key 才能访问。

```bat
python tools\admin.py key new --note "我的笔电"
```

输出：

```
====================================================================
  新 API Key（明文只显示这一次，立刻保存）：
  mvk_0123456789abcdef0123456789abcdef0123456789abcdef
====================================================================
```

> **明文只在创建时显示这一次**，库里只存 sha256。丢了就删掉重建。

管理已有 key（`<id>` 是列表里的 12 位前缀，可只写前几位）：

```bat
python tools\admin.py key list
python tools\admin.py key disable a1b2c3d4e5f6
python tools\admin.py key enable  a1b2c3d4e5f6
python tools\admin.py key delete  a1b2c3d4e5f6
```

---

## 客户端怎么接

创建 key 之后，在任意支持 OpenAI 协议的客户端里填：

| 项 | 值 |
|---|---|
| Base URL（OpenAI 协议） | `http://127.0.0.1:8088/v1` |
| Base URL（Anthropic 协议） | `http://127.0.0.1:8088` |
| API Key | 上一步 `key new` 打出来的 `mvk_...` |

用 `curl` 验证：

```bat
curl http://127.0.0.1:8088/v1/models -H "Authorization: Bearer mvk_..."
```

也可以用 `X-API-Key: mvk_...` 头，两种都认。

> 还没创建任何 key 时，带任意字符串（甚至不带）都能访问，方便先跑起来。


---

## 一键接线（把本机 agent 指到本网关）

不用手工改配置文件：`/v1/wire/*` 会替你改好，改之前把原文件存到 `wire-backup/`，
随时能一键回滚。控制台「接线」页就是这套接口的图形版。

| agent | 改的配置文件 |
|---|---|
| Claude Code | `~/.claude/settings.json` |
| Codex | `~/.codex/config.toml` |
| DSH | `~/.dsh/profiles/<profile>/cordis.patch.yml` |
| zcode | 它自己的 `provider_config.json` |

装上哪个改哪个，没装就跳过并在 `not_installed` 里列出来。

```bat
:: 看装了哪些、现在接没接
curl -s http://127.0.0.1:8088/v1/wire/agents

:: 接线（写文件）
curl -s -X POST http://127.0.0.1:8088/v1/wire/apply -H "Content-Type: application/json" ^
  -d "{\"model\":\"claude-sonnet-4-5\"}"

:: 回滚
curl -s -X POST http://127.0.0.1:8088/v1/wire/rollback -H "Content-Type: application/json" -d "{}"
```

三点要知道的：

- **写进配置的 key**：请求体不给 `key` 时，服务端会自己发一把**接入钥匙**（`mkx_` 开头）
  写进去。它是库里的一把真 key，agent 能直接拿它说话；它不出现在「密钥」页，也不用管，
  删了下次接线会重新发一把。想指定某一把（比如为了能单独停掉某个工具的对话），
  在请求体里加 `"key": "mvk_..."`。
- **多 profile 的 DSH**：机器上有好几个 `~/.dsh/profiles/*` 时会按 `cordis.patch.yml` 的
  修改时间挑最近的那个，并把选择依据写进返回的 `warnings`。想指定就在请求体里加
  `"profile": "desktop"`。
- **改错了**：`/v1/wire/rollback` 按 `wire-backup/` 里的原件逐字节还原，不是「猜着改回去」。

---

## 控制台

`console/static/` 是一个不打包、不构建、没有依赖的静态页（普通 ES module + 一份 CSS），
浏览器打开 <http://127.0.0.1:8088/> 就是它。

| 页面 | 看什么 |
|---|---|
| 概览 | 链路是否健康、池子还剩多少、刚才慢在哪；清理 / 备份 / 续期 / 看原始链路 |
| 账号池 | 加号、探测、续期、禁用、删除 |
| 用量 | 请求量、token 构成、延迟分位，按天 / 模型 / 账号切 |
| 模型 | 客户端真正看到的清单（多个名字落同一槽位是刻意的） |
| 接线 | 把本机 agent 指到本网关；回滚 |
| 密钥 | 建 / 停 / 删客户端 key，明文只显示一次 |
| 路由 | 路由图（此刻会送到谁）+ 请求流水（刚才送到了谁） |

管理令牌存在浏览器 `localStorage` 的 `marvis.admin_token`（侧栏底部可填）。`admin_token`
为空时服务端本机免鉴权，填不填都能用；一旦设了就要填，否则除 `/health` 和 `/v1/models`
之外的接口都会 401。

---

## 依赖

| 层面 | 需要什么 | 谁来准备 |
|---|---|---|
| 主程序运行 | Python 3.8+ 标准库 | 无额外依赖 |
| 出票（换 API Key） | PC `beacon_wrapper.dll`<br>手机 `tools/bionic_shim/*.so`（已随包） | PC 用 `tools\extract_marvis_dll.py` 一次性提取 |
| 设备指纹 / 应用标识 | 无需任何东西 | 首次启动自动读取 |

设备指纹 `qimei36` 由 `cap/autoconfig.py` 扫本机客户端数据文件取得（约 0.2 秒），应用标识
`appKey` 是应用级常量（所有人相同，已写在代码里）。**两者都不需要 Node.js**，只有在扫不到
`qimei36` 时才会尝试走 Node.js 现算；没有 Node 也不影响使用，手填一次即可。

---

## 目录

```
serve.py              HTTP 入口 + 路由
run.bat / run.sh      启动壳（Windows / POSIX），参数一一对应
backup.bat / .sh      备份壳
watchdog.sh           POSIX 看门狗
config.json           主配置
models.json           模型映射
sanitize.json         脱敏规则

api/                  协议翻译 + 管理接口（openai / anthropic / admin / keys / wire）
core/                 基础设施（pool 账号调度 / store / tasks / backup）
cap/                  上游协议（chat / image / login / guard / device / wallet /
                      uskey 出票调度 / uskey_local / uskey_pure / autoconfig）
console/static/       控制台前端（index.html + console.css + 8 个页面模块，不打包不构建）
tools/                工具集（自检、验收、备份、管理、提取 DLL、取指纹、抓包解析、
                      生图 MCP、bionic_shim 手机出票资产）
capture/              抓包投放位（故意为空）
data/ logs/ backups/ native/    运行时数据与本地资产（不入版本管理）
```

---

## 配置

`config.json` 所有项都有默认值，不填也能跑。常用的几项：

| 键 | 默认 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | 监听地址，局域网访问填 `0.0.0.0` |
| `port` | `8088` | 端口 |
| `uskey_backend` | `auto` | 出票通道 |
| `uskey_mint` | `true` | 是否真出票 |
| `send_marvis_ext` | `true` | 是否发送设备指纹头 |
| `marvis_qimei36` | `""` | 手填设备指纹（留空则自动获取） |

环境变量优先级高于配置文件：

| 变量 | 作用 |
|---|---|
| `MARVIS_PORT` / `MARVIS_DATA_DIR` | 覆盖 `port` / `data_dir` |
| `MARVIS_CTX_GETTER_RVA` | 出票函数偏移（一般不用动，自动探测） |
| `MARVIS_QIMEI_APPKEY` | QIMEI 应用标识 |
| `QIMEI_NODE` | 指定 node 可执行文件路径 |

---

## 出票通道

`uskey_backend: "auto"` 时按下面顺序依次尝试，第一个成功即用：

| 顺序 | 通道 | 平台 | 需要什么 | 耗时 |
|---|---|---|---|---|
| 1 | `ctypes` | Windows | `native/beacon_wrapper.dll` | 约 0.33 ms |
| 2 | `local` | Android | `tools/bionic_shim/*.so`（已随包） | 约 0.4 ms |
| 3 | `pure` | 任意 | 纯 Python（**公开版为骨架，不含实现**） | 约 3.5 ms |
| 4 | `node` | 任意 | `beacon_napi.node` | — |

`run.bat status` 会打印类似：

```
detail      : {'ctypes': True, 'pure': False, 'node': False, 'local': False}
选中 backend: ctypes
```

> **`pure` 为空是正常的。** 这一档是「连客户端都没有时的兜底」，需要票据的完整生成算法
> 和若干固定常量，那部分不随本仓库分发。想自己实现可在 `cap/uskey_pure.py` 里
> 补 `mint_ticket()`。**但通常你不需要它**：Windows 用 `ctypes`，Android 用 `local`。

---

## 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `503 no_account` | 没加号 |
| `403` + `4100404` | 上游风控。依次查：①`data/qimei36.txt` 是否真实值 ②`run.bat status` 看 `ctypes`/`local` 是否 `True` ③账号是否过期 |
| `403` + `5104002` | 上游 RPC 依赖失败 |
| `ctypes: False` + `win32-native not found` | 没找到客户端，跑 `python tools\extract_marvis_dll.py` |
| `ctypes: False` + `beacon_init rc=...` | DLL 与客户端版本不匹配 |
| `pure: False` | 正常，公开版不含实现 |
| 端口被占用 | `run.bat stop`，或改 `port` / 设 `MARVIS_PORT` |
| `UnicodeEncodeError` | `set PYTHONIOENCODING=utf-8` |
| 手机 `.so` 加载失败 | `/sdcard` 是 FUSE 文件系统，`.so` 在里面 `dlopen` 会报 `failed to map segment from shared object`。`run.sh` 会自动拷到真实文件系统再加载 |

---

## 贡献

欢迎 PR。提交前请注意：

- **不要提交 `data/` 下任何内容**（含设备指纹、密钥、数据库）
- **不要提交 `native/` 下的 DLL**（上游二进制，且与账号绑定）
- 跑一遍 `python tools\selfcheck.py`，确保没有新增 FAIL

---

## 许可

见 [LICENSE](LICENSE)
