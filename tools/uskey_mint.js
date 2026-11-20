// uskey 出票 worker（常驻）：从 stdin 逐行读 JSON，向 stdout 逐行回 JSON。
//
// 每条输入：{"native":"<dir>","appkey":"...","appver":"...","common":{...},"busid":"marvis","businfo":"..."}
// 每条输出：{"uskey":"..."} 或 {"error":"..."}
//
// 用常驻进程的原因：require() 一个 3.7MB 的 .node 插件 + 首次 init 要几百毫秒，
// 每条请求都起 node 进程会把对话延迟抬高 0.5~1s。
const path = require('path');

const argv = JSON.parse(process.argv[2] || '{}');
const native = argv.native;
let mod = null;
let inited = false;
let commonSet = false;
let common = null;

function load() {
  if (!mod) {
    mod = require(path.join(native, 'beacon_napi.node'));
  }
  if (!inited) {
    try { mod.init(argv.appkey, argv.appver); } catch (e) { }
    inited = true;
  }
  if (!commonSet && argv.common) {
    try { mod.setCommonParams(argv.common); } catch (e) { }
    common = argv.common;
    commonSet = true;
  }
  return mod;
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (!line) continue;
    let out;
    try {
      const req = JSON.parse(line);
      const m = load();
      if (req.common && JSON.stringify(req.common) !== JSON.stringify(common)) {
        try { m.setCommonParams(req.common); } catch (e) { }
        common = req.common;
      }
      const u = m.getUskey(req.busid || 'marvis', req.businfo || '');
      out = { uskey: u || '' };
    } catch (e) {
      out = { error: String(e) };
    }
    process.stdout.write(JSON.stringify(out) + '\n');
  }
});

process.stdin.on('end', () => process.exit(0));
