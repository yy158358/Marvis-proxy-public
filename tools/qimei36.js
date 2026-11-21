// 用官方客户端自带的 qimei_napi.node 取本机真实 QIMEI36（与客户端同源同值）。
//
// 取值的模块来自官方客户端安装目录：
//   <安装目录>\Application\<ver>\win32-native\qimei_napi.node
//   它导出 init / getQ36 / getQ16 / getToken / setReadyCallback / waitReady / shutdown / hardExit
//   签名：init(appKey, appVersion, [dllPath], [guid]) —— 前两个必填
//
// 用法： node tools\qimei36.js [--native-dir <win32-native>] [--app-version 1.60.3400.35] [--guid <32位>]
"use strict";
const path = require("path");
const fs = require("fs");

function arg(name, def) {
  const i = process.argv.indexOf("--" + name);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

function candidateDirs() {
  const out = [];
  const roots = new Set([
    process.env["ProgramFiles"] || "C:/Program Files",
    process.env["ProgramW6432"] || "C:/Program Files",
    process.env["ProgramFiles(x86)"] || "C:/Program Files (x86)",
  ]);
  // 客户端不一定装在系统盘（本机在 E:），所以把各盘的 Program Files 都找一遍
  for (const d of "CDEFGHIJ") {
    roots.add(d + ":/Program Files");
    roots.add(d + ":/Program Files (x86)");
  }
  for (const r of roots) {
    const appDir = path.join(r, "Tencent", "Marvis", "Application");
    if (!fs.existsSync(appDir)) continue;
    for (const v of fs.readdirSync(appDir)) {
      out.push(path.join(appDir, v, "win32-native"));
    }
  }
  return out;
}

function findAddon() {
  const given = arg("native-dir", "");
  for (const d of given ? [given] : candidateDirs()) {
    const p = path.join(d, "qimei_napi.node");
    if (fs.existsSync(p)) return p;
  }
  throw new Error("找不到 qimei_napi.node（用 --native-dir 指定 win32-native 目录）");
}

const addonPath = findAddon();
const nativeDir = path.dirname(addonPath);
const dllPath = arg("dll", path.join(nativeDir, "qimei.dll"));
// appKey 是 Marvis 客户端注册的应用标识，不能自行生成；
// 可用 --app-key 或环境变量 MARVIS_QIMEI_APPKEY 覆盖。
const appKey = arg("app-key", process.env["MARVIS_QIMEI_APPKEY"] || "0WIN06Y06DKV1IUQ");
const appVersion = arg("app-version", "1.60.3400.35");
const guid = arg("guid", "");

const m = require(addonPath);
let rc;
try {
  rc = guid ? m.init(appKey, appVersion, dllPath, guid) : m.init(appKey, appVersion, dllPath);
} catch (e) {
  console.error("init 失败: " + String(e.message || e).split("\n")[0]);
  process.exit(2);
}
const q36 = m.getQ36();
const q16 = m.getQ16();
process.stdout.write(JSON.stringify({
  ok: rc === 0, rc, appKey, appVersion,
  dll: dllPath, guid: guid || null, qimei36: q36, qimei16: q16,
}) + "\n");
try { m.shutdown(); } catch (e) { /* 忽略 */ }
