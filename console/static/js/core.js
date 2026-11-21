/* ============================================================
   core.js —— 接口访问、格式化、DOM 与图形基元
   ============================================================ */

const TOKEN_KEY = 'marvis.admin_token';
const CLIENT_KEY = 'marvis.client_key';

export const store = {
  get adminToken() { return localStorage.getItem(TOKEN_KEY) || ''; },
  set adminToken(v) { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); },
  get clientKey() { return localStorage.getItem(CLIENT_KEY) || ''; },
  set clientKey(v) { v ? localStorage.setItem(CLIENT_KEY, v) : localStorage.removeItem(CLIENT_KEY); },
};

/* ---------------- 接口 ---------------- */

class ApiError extends Error {
  constructor(status, message, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}
export { ApiError };

async function request(method, path, body, extraHeaders) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (store.adminToken) headers['X-Admin-Token'] = store.adminToken;
  Object.assign(headers, extraHeaders || {});
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, '连不上服务：' + (e && e.message ? e.message : '网络错误'), null);
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = { raw: text }; }
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || ('HTTP ' + res.status);
    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent('marvis:unauthorized'));
    }
    throw new ApiError(res.status, typeof msg === 'string' ? msg : JSON.stringify(msg), data);
  }
  return data;
}

export const api = {
  get: (path, headers) => request('GET', path, undefined, headers),
  // body 传 undefined = 完全不发请求体。
  // 有些服务端分支压根不读 body（备份/清理/key 动作），发了也白丢。
  post: (path, body, headers) => request('POST', path, body, headers),
};

/* ---------------- 文本安全 ---------------- */

export function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ---------------- 格式化 ---------------- */

export function fmtInt(v) {
  const n = Number(v) || 0;
  return n.toLocaleString('en-US');
}

export function fmtCompact(v) {
  const n = Number(v) || 0;
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1).replace(/\.0$/, '') + 'B';
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'K';
  return String(Math.round(n));
}

export function fmtMs(v) {
  const n = Number(v) || 0;
  if (n <= 0) return '0';
  if (n < 1000) return Math.round(n) + 'ms';
  if (n < 60000) return (n / 1000).toFixed(n < 10000 ? 2 : 1) + 's';
  return (n / 60000).toFixed(1) + 'm';
}

export function fmtBytes(v) {
  const n = Number(v) || 0;
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

export function fmtPct(v, digits) {
  const n = Number(v) || 0;
  return n.toFixed(digits === undefined ? 1 : digits) + '%';
}

export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtClock(ts) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtAgo(ts) {
  if (!ts) return '从未';
  const d = Number(ts) * 1000 - Date.now();
  // 服务器和浏览器之间差个几秒很常见。刚刚发生过的事被写成「35 秒后」是显示错误，
  // 不是事实——这种情况一律按「刚刚」报，别把时钟差说给用户看。
  if (d < 0 && d > -120000) return '刚刚';
  const abs = Math.abs(d);
  const fut = d < 0;
  const s = Math.round(abs / 1000);
  const mk = (n, unit) => (fut ? n + unit + '后' : n + unit + '前');
  if (s < 60) return mk(s, '秒');
  const m = Math.round(s / 60);
  if (m < 60) return mk(m, '分钟');
  const h = Math.round(m / 60);
  if (h < 24) return mk(h, '小时');
  return mk(Math.round(h / 24), '天');
}

export function fmtUptime(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}天 ${h}时`;
  if (h) return `${h}时 ${m}分`;
  return `${m}分 ${s % 60}秒`;
}

/* ---------------- DOM ---------------- */

/** 由 HTML 字符串建节点（模板片段） */
export function h(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/** 由 HTML 字符串建片段 */
export function frag(html) {
  const t = document.createElement('template');
  t.innerHTML = html;
  return t.content;
}

export function on(root, selector, event, handler) {
  root.addEventListener(event, (e) => {
    const t = e.target.closest(selector);
    if (t && root.contains(t)) handler(e, t);
  });
}

/* ---------------- 吐司 ---------------- */

export function toast(msg, kind) {
  const stack = document.getElementById('toasts');
  if (!stack) return;
  const el = h(`<div class="toast${kind ? ' is-' + kind : ''}">${esc(msg)}</div>`);
  stack.appendChild(el);
  setTimeout(() => {
    el.classList.add('is-out');
    setTimeout(() => el.remove(), 240);
  }, kind === 'bad' ? 6200 : 3200);
}

export function toastError(e) {
  toast(e && e.message ? e.message : String(e), 'bad');
}

/* ---------------- 弹窗 ---------------- */

export function openModal({ title, body, actions }) {
  const root = document.getElementById('modal-root');
  document.getElementById('modal-title').textContent = title;
  const bodyEl = document.getElementById('modal-body');
  bodyEl.innerHTML = '';
  bodyEl.appendChild(typeof body === 'string' ? h(`<div>${body}</div>`) : body);

  const footEl = document.getElementById('modal-foot');
  footEl.innerHTML = '';
  (actions || []).forEach((a) => {
    const b = h(`<button class="btn ${a.kind || 'btn-ghost'}" type="button">${esc(a.label)}</button>`);
    b.addEventListener('click', () => a.onClick && a.onClick(b));
    footEl.appendChild(b);
  });

  root.hidden = false;
  const focusable = root.querySelector('input, textarea, select, button');
  if (focusable) setTimeout(() => focusable.focus(), 40);
  return { root, bodyEl, footEl };
}

export function closeModal() {
  const root = document.getElementById('modal-root');
  if (root) root.hidden = true;
}

export function initModal() {
  const root = document.getElementById('modal-root');
  root.addEventListener('click', (e) => {
    if (e.target.dataset && e.target.dataset.close) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !root.hidden) closeModal();
  });
}

/* ---------------- 复制 ---------------- */

export async function copyText(text, label) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast((label || '内容') + '已复制', 'good');
    return true;
  } catch (e) {
    toast('复制失败，手动选中吧', 'bad');
    return false;
  }
}

export function copyBox(text, label) {
  const el = h(
    `<div class="copybox"><code>${esc(text)}</code>` +
    `<button type="button">复制</button></div>`
  );
  el.querySelector('button').addEventListener('click', () => copyText(text, label));
  return el;
}

/* ---------------- 分段控件 ---------------- */

/**
 * Magpie 式分段控件：一条凹槽轨道 + 一枚会滑动的纸片拇指。
 * 整块换色看不出「滑到哪一格」，拇指自己走过去，手才跟得上。
 * @param {{v:string|number,label:string}[]} items
 * @param {string|number} value 初始值
 * @param {(v:string)=>void} onChange
 */
export function segmented(items, value, onChange) {
  const root = h(`<div class="seg" role="tablist"></div>`);
  const thumb = h(`<span class="seg-thumb" aria-hidden="true"></span>`);
  root.appendChild(thumb);

  const btns = items.map((it) => {
    const b = h(`<button type="button" role="tab" data-v="${esc(it.v)}">${esc(it.label)}</button>`);
    root.appendChild(b);
    return b;
  });

  let cur = value;

  function paint() {
    btns.forEach((b) => {
      const on = b.dataset.v === String(cur);
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-selected', String(on));
    });
  }

  /** 把拇指挪到当前项上。instant=true 时不播滑动动画（首帧 / 尺寸变化）。 */
  function place(instant) {
    const btn = btns.find((b) => b.dataset.v === String(cur));
    if (!btn) return;
    const cs = getComputedStyle(root);
    const bl = parseFloat(cs.borderLeftWidth) || 0;
    const bt = parseFloat(cs.borderTopWidth) || 0;
    const sr = root.getBoundingClientRect();
    const br = btn.getBoundingClientRect();
    if (!br.width) return;   // 还没进 DOM，等 ResizeObserver 再来一次
    if (instant) thumb.style.transition = 'none';
    // 拇指只包住文字，不吞掉整格（Magpie 那边也是这么收的）
    thumb.style.width = Math.max(8, br.width - 16) + 'px';
    thumb.style.height = Math.max(8, br.height - 4) + 'px';
    thumb.style.transform =
      `translate(${(br.left - sr.left - bl + 8).toFixed(1)}px, ${(br.top - sr.top - bt + 2).toFixed(1)}px)`;
    if (instant) {
      void thumb.offsetWidth;   // 强制回流，让「无动画」这一帧生效
      thumb.style.transition = '';
    }
  }

  root.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-v]');
    if (!b || !root.contains(b)) return;
    const v = b.dataset.v;
    if (v === String(cur)) return;
    cur = v;
    paint();
    place(false);
    if (onChange) onChange(v);
  });

  paint();
  requestAnimationFrame(() => place(true));
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => place(true));
    ro.observe(root);
  }

  return {
    node: root,
    get value() { return cur; },
    setValue(v) { cur = v; paint(); place(false); },
    relayout() { place(true); },
  };
}

/* ---------------- 数字滚动 ---------------- */

export function countUp(el, to, fmt) {
  const from = Number(el.dataset.value || 0);
  const target = Number(to) || 0;
  el.dataset.value = String(target);
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el.textContent = fmt ? fmt(target) : fmtCompact(target);
    return;
  }
  const dur = 620;
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    const v = from + (target - from) * eased;
    el.textContent = fmt ? fmt(v) : fmtCompact(v);
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ---------------- 图形基元 ---------------- */

/**
 * 延迟波形：把一串毫秒画成示波带。
 * @param {number[]} values 延迟序列（毫秒），从旧到新
 * @param {{height?:number, width?:number, p95?:number, median?:number, label?:string}} opt
 */
export function waveform(values, opt) {
  const o = Object.assign({ height: 190, width: 1000, pad: 14 }, opt || {});
  const vals = (values || []).filter((v) => Number.isFinite(v));
  if (vals.length < 2) {
    return `<div class="empty"><div class="empty-title">还没有足够的样本</div>
      <div class="empty-text">波形需要至少两轮请求。等上游跑过一会儿，这里会自己长出来。</div></div>`;
  }

  const maxV = Math.max(...vals, o.p95 || 0) * 1.12 || 1;
  const n = vals.length;
  const innerW = o.width - o.pad * 2;
  const innerH = o.height - o.pad * 2;

  const X = (i) => o.pad + (i / (n - 1)) * innerW;
  const Y = (v) => o.pad + innerH - (v / maxV) * innerH;

  let line = '';
  for (let i = 0; i < n; i++) {
    line += (i ? ' L' : 'M') + X(i).toFixed(2) + ' ' + Y(vals[i]).toFixed(2);
  }
  const area = line + ` L${X(n - 1).toFixed(2)} ${(o.pad + innerH).toFixed(2)} L${X(0).toFixed(2)} ${(o.pad + innerH).toFixed(2)} Z`;

  // 参考横线
  const gridLines = [0.25, 0.5, 0.75].map((f) => {
    const y = (o.pad + innerH * f).toFixed(1);
    return `<line x1="${o.pad}" y1="${y}" x2="${o.width - o.pad}" y2="${y}" stroke="rgba(25,23,20,0.10)" stroke-width="1"/>`;
  }).join('');

  const medianLine = o.median ? `
    <line x1="${o.pad}" y1="${Y(o.median).toFixed(1)}" x2="${o.width - o.pad}" y2="${Y(o.median).toFixed(1)}"
      stroke="rgba(25,23,20,0.42)" stroke-width="1" stroke-dasharray="5 4"/>
    <text x="${o.pad + 4}" y="${(Y(o.median) - 5).toFixed(1)}"
      font-family="IBM Plex Mono, monospace" font-size="10" fill="#514C42">中位 ${esc(fmtMs(o.median))}</text>` : '';

  const p95Line = o.p95 ? `
    <line x1="${o.pad}" y1="${Y(o.p95).toFixed(1)}" x2="${o.width - o.pad}" y2="${Y(o.p95).toFixed(1)}"
      stroke="#C43F27" stroke-width="1" stroke-dasharray="3 3" opacity="0.75"/>
    <text x="${o.pad + 4}" y="${(Y(o.p95) - 5).toFixed(1)}"
      font-family="IBM Plex Mono, monospace" font-size="10" fill="#C43F27">p95 ${esc(fmtMs(o.p95))}</text>` : '';

  // 末点标记
  const lastX = X(n - 1), lastY = Y(vals[n - 1]);
  const endMark = `
    <circle cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="3.2" fill="#C43F27"/>
    <circle cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="6.5" fill="none" stroke="#C43F27" opacity="0.3"/>`;

  // 峰值标记
  let peak = '';
  let peakI = 0;
  vals.forEach((v, i) => { if (v > vals[peakI]) peakI = i; });
  if (vals[peakI] > 0) {
    peak = `<circle cx="${X(peakI).toFixed(2)}" cy="${Y(vals[peakI]).toFixed(2)}" r="2.4" fill="#191714" opacity="0.55"/>`;
  }

  return `<svg viewBox="0 0 ${o.width} ${o.height}" width="${o.width}" height="${o.height}"
      preserveAspectRatio="xMidYMid meet" role="img"
      aria-label="请求延迟波形，共 ${n} 次请求">
    <defs>
      <linearGradient id="wfFade" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#C43F27" stop-opacity="0.16"/>
        <stop offset="100%" stop-color="#C43F27" stop-opacity="0"/>
      </linearGradient>
    </defs>
    ${gridLines}
    <path d="${area}" fill="url(#wfFade)"/>
    <path class="trace-line" pathLength="1" d="${line}" fill="none" stroke="#C43F27"
      stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
    ${medianLine}
    ${p95Line}
    ${peak}
    ${endMark}
  </svg>`;
}

/** 竖条图：请求量柱 + 可选的一条折线（p95 延迟）。
 *  柱用单一颜色；折线用另一把尺（右轴），两件事各说各的。 */
export function bars(items, opt) {
  const o = Object.assign(
    { height: 210, width: 1000, pad: 14, label: 'req', color: '#8B8477', showX: false },
    opt || {}
  );
  if (!items || !items.length) {
    return `<div class="empty"><div class="empty-title">区间内没有记录</div>
      <div class="empty-text">换一个时间窗口，或者先让上游跑几轮。</div></div>`;
  }
  const n = items.length;
  const maxV = Math.max(...items.map((d) => d.value)) || 1;
  const labelBand = o.showX ? 20 : 0;
  const innerW = o.width - o.pad * 2;
  const innerH = o.height - o.pad * 2 - labelBand;
  const baseY = o.pad + innerH;
  const slot = innerW / n;
  const bw = Math.max(2, Math.min(slot * 0.62, 58));

  // x 轴标签抽稀：每 46px 最多一个，否则 30 天 / 90 天会糊成一片。
  // 首尾必标，末位若与前一个挤在一起就把前一个让掉。
  const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(innerW / 46))));
  const labeled = new Set();
  for (let i = 0; i < n; i += step) labeled.add(i);
  if (n > 1) {
    const last = n - 1;
    if (!labeled.has(last)) {
      const prev = Math.max(...labeled);
      if (last - prev < step * 0.6) labeled.delete(prev);
      labeled.add(last);
    }
  }

  let out = '';

  // 参考横线 + 峰值刻度
  [0.5, 1].forEach((f) => {
    const y = (baseY - innerH * f).toFixed(1);
    out += `<line x1="${o.pad}" y1="${y}" x2="${o.width - o.pad}" y2="${y}"
      stroke="rgba(25,23,20,${f === 1 ? 0.22 : 0.09})" stroke-width="1"/>`;
  });
  // 峰值刻度：左轴（请求数）放左边，右轴留给下面的 p95 折线。
  out += `<text x="${o.pad}" y="${(baseY - innerH + 9).toFixed(1)}"
    font-family="IBM Plex Mono, monospace" font-size="10" fill="#8B8477">${esc(fmtCompact(maxV))}</text>`;

  items.forEach((d, i) => {
    const x = o.pad + slot * i + slot / 2;
    const h = Math.max(1.5, (d.value / maxV) * innerH);
    const y = baseY - h;
    out += `<g class="bar-g">
      <rect x="${(x - bw / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}"
        height="${h.toFixed(1)}" fill="${o.color}"/>
      <title>${esc(d.label)} · ${esc(fmtCompact(d.value))} ${esc(o.label)}</title></g>`;

    if (o.showX && d.xLabel && labeled.has(i)) {
      out += `<text x="${x.toFixed(1)}" y="${(baseY + 14).toFixed(1)}" text-anchor="middle"
        font-family="IBM Plex Mono, monospace" font-size="10" fill="#8B8477">${esc(d.xLabel)}</text>`;
    }
  });

  // 折线：右轴，自成一套刻度
  if (o.line && o.line.length) {
    const lmax = Math.max(...o.line.map((v) => v)) || 1;
    const lmin = Math.min(...o.line.map((v) => v));
    const span = lmax - lmin || lmax || 1;
    const lbase = lmin - span * 0.35;
    const lspan = lmax - lbase || 1;
    const LY = (v) => baseY - ((v - lbase) / lspan) * innerH;

    let d = '';
    o.line.forEach((v, i) => {
      const x = (o.pad + slot * i + slot / 2).toFixed(1);
      const y = LY(v).toFixed(1);
      d += (i ? ' L' : 'M') + x + ' ' + y;
    });
    out += `<path d="${d}" fill="none" stroke="${o.lineColor || '#C43F27'}"
      stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>`;
    o.line.forEach((v, i) => {
      const x = (o.pad + slot * i + slot / 2).toFixed(1);
      const y = LY(v).toFixed(1);
      out += `<circle cx="${x}" cy="${y}" r="2.6" fill="${o.lineColor || '#C43F27'}"/>`;
    });
    out += `<text x="${o.width - o.pad}" y="${(baseY - innerH + 9).toFixed(1)}" text-anchor="end"
      font-family="IBM Plex Mono, monospace" font-size="10" fill="${o.lineColor || '#C43F27'}">${esc(o.lineLabel || '')} ${esc(fmtMs(lmax))}</text>`;
  }

  out += `<line x1="${o.pad}" y1="${baseY}" x2="${o.width - o.pad}" y2="${baseY}"
    stroke="rgba(25,23,20,0.30)" stroke-width="1"/>`;

  return `<svg viewBox="0 0 ${o.width} ${o.height}" width="${o.width}" height="${o.height}"
    preserveAspectRatio="xMidYMid meet" role="img"
    aria-label="${esc(o.label)} 分布">${out}</svg>`;
}

/** 行内迷你折线 */
export function sparkline(values, opt) {
  const o = Object.assign({ width: 108, height: 26, color: '#12696E' }, opt || {});
  const vals = (values || []).filter((v) => Number.isFinite(v));
  if (vals.length < 2) return `<span class="cell-mute">—</span>`;
  const max = Math.max(...vals) || 1;
  const min = Math.min(...vals);
  const span = max - min || 1;
  const step = o.width / (vals.length - 1);
  let d = '';
  vals.forEach((v, i) => {
    const x = (i * step).toFixed(1);
    const y = (o.height - 2 - ((v - min) / span) * (o.height - 4)).toFixed(1);
    d += (i ? ' L' : 'M') + x + ' ' + y;
  });
  return `<svg viewBox="0 0 ${o.width} ${o.height}" width="${o.width}" height="${o.height}"
    preserveAspectRatio="none" aria-hidden="true" style="display:block">
    <path d="${d}" fill="none" stroke="${o.color}" stroke-width="1.3" stroke-linejoin="round"/></svg>`;
}

/** 令牌构成堆叠条 */
export function tokenBar(prompt, completion, reasoning) {
  const p = Number(prompt) || 0;
  const c = Number(completion) || 0;
  const r = Number(reasoning) || 0;
  const total = p + c + r || 1;
  return `<div class="stackbar">
    <i class="seg-prompt" style="width:${(p / total * 100).toFixed(2)}%"></i>
    <i class="seg-completion" style="width:${(c / total * 100).toFixed(2)}%"></i>
    <i class="seg-reasoning" style="width:${(r / total * 100).toFixed(2)}%"></i>
  </div>`;
}

/** 统计工具 */
export function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const i = Math.floor(sorted.length * q);
  return sorted[Math.min(i, sorted.length - 1)];
}

/** 状态标签 */
export function tag(text, kind) {
  return `<span class="tag tag-${kind || 'idle'}">${esc(text)}</span>`;
}

/** 带 LED 的状态标签 */
export function ledTag(text, kind) {
  const fill = kind === 'ok' ? 'fill-ok' : kind === 'bad' ? 'fill-bad' : kind === 'warn' ? 'fill-warn' : 'fill-off';
  return `<span class="tag tag-${kind || 'idle'}"><i class="led ${fill}"></i>${esc(text)}</span>`;
}
