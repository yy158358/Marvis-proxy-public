/* ============================================================
   pages/routing.js —— 路由
   智能体 → 代理 → 账号池。此刻请求会被送到谁、凭什么送过去。

   布局参照 Magpie 的路由视图：
     顶部一排页签 + 右侧一段解释
     主图三段：智能体 → 代理 → 候选列表（每行右端写「为什么是它」）
     底部一条读数带
   但页签切的是「看判定链的第几步」，不是切换策略——
   Marvis 只有一套调度，三步是它的固定顺序，不做假的策略开关。

   画的都是真路由：
   · 候选集合直接来自后端 pool._candidates，与真实调度同一条路径
   · 中签份额由后端按 pool._choose 的同一口径算出
   · 高亮的一号 = 上一轮请求的实际落点
   ============================================================ */

import {
  api, h, esc, fmtInt, fmtPct, fmtMs, fmtAgo, segmented, countUp, toast, toastError,
} from '../core.js';
import flowPage from './trace.js';

const VIEWS = [
  { v: 'route', label: '路由图' },
  { v: 'flow', label: '请求流水' },
];

/* 判定链三步。顺序固定：pool.pick 先看粘性，再看负载，最后看权重。 */
const CHAIN = [
  { v: 'sticky', label: '会话粘性' },
  { v: 'load', label: '最小负载' },
  { v: 'weight', label: '权重加权' },
];

/* 智能体状态 → 圆点颜色 + 副标。off/stale/absent 都是「还没接上」，live 才是通的。 */
const CLIENT_STATE = {
  live:   { text: '已接线',   cls: 'is-live' },
  stale:  { text: '被改回去了', cls: 'is-warn' },
  off:    { text: '未接线',   cls: 'is-mute' },
  absent: { text: '未安装',   cls: 'is-mute' },
};

/* 备注可能很长（「续期失败（请求异常：…），需重新加号：POST /admin/login/start」），
   卡面上只留能认出是谁的那一小截，完整的放在 title 里。 */
function shortNote(note) {
  const s = (note || '').trim();
  if (!s) return '—';
  if (s.length <= 22) return s;
  return s.slice(0, 22) + '…';
}

/* 每行右端的「为什么是它」——随页签变。
   格式是 Magpie 那种两段式：「分类 · 说明」。
   分类是这一步的依据（粘性 / 负载 / 权重），不可用的号改说自己的问题。
   上一轮真正在服务的那一号，第一段换成「服务中」——那才是眼下最要紧的事实。 */
function reasonOf(a, step, hopId) {
  if (!a.usable) {
    const kind = a.reason_kind || '不可用';
    return {
      text: kind + ' · ' + (a.reason_short || a.reason || ''),
      cls: 'is-bad',
    };
  }
  const on = a.id === hopId;
  const tag = on ? '服务中' : '';
  if (step === 'sticky') {
    const n = Number(a.sticky) || 0;
    const body = n > 0 ? '粘住 ' + fmtInt(n) + ' 个会话' : '没有粘住会话';
    return { text: tag ? tag + ' · ' + body : '粘性 · ' + body, cls: on ? 'is-live' : n > 0 ? 'is-live' : 'is-mute' };
  }
  if (step === 'load') {
    const body = a.in_tied
      ? '在途 ' + fmtInt(a.inflight) + ' 并列最闲'
      : '在途 ' + fmtInt(a.inflight) + ' 落后';
    return { text: tag ? tag + ' · ' + body : '负载 · ' + body, cls: on || a.in_tied ? 'is-live' : 'is-mute' };
  }
  // weight
  const p = Number(a.win_prob) || 0;
  const body = p > 0 ? '中签 ' + fmtPct(p, 1) : '不在并列集';
  return { text: tag ? tag + ' · ' + body : '权重 · ' + body, cls: on || p > 0 ? 'is-live' : 'is-mute' };
}

/* 排序也跟着页签走：这一页要突出的是这一轮真正起作用的那个因子。
   但上一轮落点永远排最前——它是此刻真正在服务的那一个。 */
function sortFor(accs, step, hopId) {
  const list = accs.slice();
  const isHop = (a) => (hopId != null && a.id === hopId ? 1 : 0);
  if (step === 'sticky') {
    list.sort((x, y) => (isHop(y) - isHop(x)) || (y.sticky - x.sticky)
      || (x.inflight - y.inflight) || (x.id - y.id));
  } else if (step === 'load') {
    list.sort((x, y) => (isHop(y) - isHop(x))
      || (Number(y.in_tied) - Number(x.in_tied))
      || (x.inflight - y.inflight) || (x.id - y.id));
  } else {
    list.sort((x, y) => (isHop(y) - isHop(x)) || (y.win_prob - x.win_prob) || (x.id - y.id));
  }
  return list;
}

/* 左列只画一个智能体。
   这一列说的是「哪个智能体在跟网关说话」：装了不等于在用，
   接了线也不等于这一轮在请求。所以只留一个节点——真正指向本网关的那一个。
   其余智能体的数量写进列头（一个真数字），不靠多画几张卡来表达。 */
function pickPrimary(list) {
  if (!list || !list.length) return null;
  // live = 配置里的地址真的指向本网关，这是「在用」的硬证据
  const live = list.filter((c) => c.state === 'live');
  if (live.length) return live[0];
  // 没有 live 的，退回装了的那一个（接线页照样能看见它没接上）
  const installed = list.filter((c) => c.installed);
  return installed[0] || list[0];
}

export default {
  sub: '智能体 → 代理 → 账号池。此刻请求会被送到谁、凭什么送过去。',

  async render() {
    const root = h(`<div class="stack"></div>`);

    let mode = 'route';
    let step = 'sticky';
    let innerCleanup = null;
    let timer = null;

    /* ---- 视图切换 ---- */
    const viewSeg = segmented(
      VIEWS.map((v) => ({ v: v.v, label: v.label })),
      mode,
      (v) => switchTo(v)
    );

    const autoBtn = h(`<button class="btn btn-ghost push-right" type="button">自动刷新：关</button>`);
    const tools = h(`<div class="toolbar"></div>`);
    tools.appendChild(viewSeg.node);
    tools.appendChild(autoBtn);
    root.appendChild(tools);

    const pane = h(`<div class="route-pane"></div>`);
    root.appendChild(pane);

    /* ---- 路由图 ---- */
    const mapWrap = h(`<div class="route-map"></div>`);
    /* 底部读数带：左边一段说明（上一轮发生了什么），右边三个读数。 */
    const footBox = h(`<div class="route-foot"><div class="route-cap"></div><div class="route-stats"></div></div>`);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'route-links');
    svg.setAttribute('aria-hidden', 'true');
    mapWrap.appendChild(svg);

    let clients = [];
    let routing = null;

    /* 顶部：页签 + 右侧解释。解释来自后端，写的是这一轮的真实规则。 */
    function paintHead() {
      const box = mapWrap.querySelector('.route-head');
      if (!box) return;
      const chain = (routing && routing.strategy && routing.strategy.chain) || [];
      const cur = chain.find((c) => c.id === step) || chain[0] || {};
      const note = box.querySelector('.route-head-note');
      if (note) note.textContent = cur.desc || '';
      const strat = box.querySelector('.route-head-strategy');
      if (strat) {
        strat.textContent = (routing && routing.strategy && routing.strategy.name) || '';
      }
    }

    function paintClients() {
      const list = mapWrap.querySelector('.route-clients-list');
      if (!list) return;
      const head = mapWrap.querySelector('[data-role="client-head"]');

      if (!clients.length) {
        list.innerHTML = '<div class="cell-mute micro">没找到任何智能体</div>';
        if (head) head.textContent = '0 个';
        return;
      }

      const c = pickPrimary(clients);
      const st = CLIENT_STATE[c.state] || CLIENT_STATE.off;
      const wired = clients.filter((x) => x.state === 'live').length;
      const sub = c.state === 'live'
        ? '已接线 · ' + ((c.current && c.current.model) || '默认')
        : st.text + (c.warnings && c.warnings.length ? ' · ' + c.warnings.length + ' 条提示' : '');

      /* 一个节点卡：图标在上、名称在中、状态在下。
         和中间那张网关卡同一个骨架，一眼就看出「谁把请求递给谁」。
         卡面写的是这一层是什么（智能体），不是某一个产品名——
         具体是 Claude Code 还是 Codex 放 title，鼠标一停就知道。 */
      const who = String(c.display_name || c.id);
      const tip = [who].concat(c.warnings || []).join('\n');
      list.innerHTML = `<div class="route-client ${st.cls}" title="${esc(tip)}">
          <span class="route-client-glyph" aria-hidden="true">${esc(c.glyph || '·')}</span>
          <b class="route-client-name">智能体</b>
          <small class="route-client-sub">${esc(sub)}</small>
        </div>`;

      /* 列头给一个真数字：装了几个、其中几个真的接上了。
         卡片只有一个，但「其余几个没在用」这件事不该被藏起来。 */
      if (head) {
        head.textContent = clients.length + ' 个 · ' + wired + ' 个已接线';
      }
    }

    function paintAccounts() {
      const list = mapWrap.querySelector('.route-accs');
      if (!list || !routing) return;
      const hop = routing.last_hop || {};
      const hopId = hop.account_id || null;
      const accs = routing.accounts || [];

      if (!accs.length) {
        list.innerHTML = '<div class="cell-mute micro">账号池是空的</div>';
        return;
      }

      const ordered = sortFor(accs, step, hopId);
      /* 一行两段，跟 Magpie 一样：
         上面是「粗名 + 灰备注 ← 为什么是它」，
         下面一条是负载条，一路拉到右端，紧跟着占比。
         名字是粗的、备注是灰的、原因是分段的——三层信息各归各位。 */
      list.innerHTML = ordered.map((a) => {
        const isHop = a.id === hopId;
        const rs = reasonOf(a, step, hopId);
        const w = Math.max(0, Math.min(100, Number(a.traffic) || 0));
        return `<div class="route-acc ${isHop ? 'is-hop' : ''} ${a.usable ? '' : 'is-off'}"
             data-acc="${esc(a.id)}" title="${esc(a.reason || a.note || '')}">
          <span class="route-radio ${isHop ? 'is-on' : ''}" aria-hidden="true"></span>
          <span class="route-acc-name">
            <b>${esc(a.label)}</b>
            <span class="route-acc-note">${esc(shortNote(a.note))}</span>
          </span>
          <span class="route-acc-why ${rs.cls}">${esc(rs.text)}</span>
          <span class="route-acc-load">
            <span class="route-acc-bar" title="近 ${esc(routing.totals.window_minutes || 15)} 分钟承接占比">
              <i style="width:${w}%"></i>
            </span>
            <span class="route-acc-pct mono">${esc(fmtPct(a.traffic, a.traffic >= 10 ? 0 : 1))}</span>
          </span>
        </div>`;
      }).join('');

      const head = mapWrap.querySelector('[data-role="acc-head"]');
      if (head) {
        const t = routing.totals || {};
        head.textContent = t.usable + ' 可用 / ' + t.total + ' 个';
      }
    }

    function paintHub() {
      const card = mapWrap.querySelector('.route-hub-card');
      if (!card || !routing) return;
      const t = routing.totals || {};
      const inflight = Number(t.inflight) || 0;

      const addr = card.querySelector('.route-hub-addr');
      if (addr) {
        addr.textContent = (routing.gateway || '')
          .replace(/^https?:\/\//, '').replace(/\/v1$/, '');
      }
      const tag = card.querySelector('.route-hub-tag');
      if (tag) tag.textContent = (routing.strategy && routing.strategy.name) || '';

      // 徽章是实时状态，不是装饰
      const badge = card.querySelector('.route-hub-badge');
      if (badge) {
        badge.textContent = inflight > 0 ? '处理中 ' + fmtInt(inflight) : '待命';
        badge.classList.toggle('is-busy', inflight > 0);
      }

      const note = mapWrap.querySelector('.route-hub-note');
      if (note) {
        note.innerHTML = `近 ${esc(t.window_minutes || 15)} 分钟 <b>${esc(fmtInt(t.requests || 0))}</b> 轮`
          + ` · 并列最闲 <b>${esc(fmtInt((routing.strategy && routing.strategy.tied_count) || 0))}</b> 个`;
      }
    }

    function paintHop() {
      const cap = footBox.querySelector('.route-cap');
      if (!cap) return;
      const hop = routing && routing.last_hop;
      if (!hop || !hop.at) {
        cap.innerHTML = '<span class="cell-mute micro">还没有请求走过这条路</span>';
        return;
      }
      const st = Number(hop.status) || 0;
      const ok = st < 400;
      /* 左边这段话讲的是「上一轮到底发生了什么」——
         模型、槽位、落在哪个号上、耗时多久、什么时候、结果如何。 */
      cap.innerHTML = `
        <span class="route-cap-label">上一轮</span>
        <span class="route-cap-body">
          <span class="route-cap-lead">${esc(hop.model || '—')}</span>
          <span class="route-cap-arrow" aria-hidden="true">→</span>
          <span class="route-cap-lead">${esc(hop.slot || '—')}</span>
          <span class="route-cap-arrow" aria-hidden="true">→</span>
          <span class="route-cap-lead">${esc(hop.label || '—')}</span>
          <span class="route-cap-meta">${esc(fmtMs(hop.ms))} · ${esc(fmtAgo(hop.at))} · ${ok ? '成功' : '失败 ' + st}</span>
        </span>`;
    }

    /* 底部读数带：右端三个大数 + 小标签。数字都是真读数，不是装饰。 */
    function paintStrip() {
      const box = footBox.querySelector('.route-stats');
      if (!box) return;
      const t = (routing && routing.totals) || {};
      const s = (routing && routing.strategy) || {};
      const items = [
        { k: 'sessions', label: '粘住会话', fmt: (v) => fmtInt(v) },
        { k: 'tied', label: '并列最闲', fmt: (v) => fmtInt(v) },
        { k: 'inflight', label: '在途请求', fmt: (v) => fmtInt(v) },
      ];
      const vals = {
        sessions: Number(t.sessions) || 0,
        tied: Number(s.tied_count) || 0,
        inflight: Number(t.inflight) || 0,
      };
      if (!box.children.length) {
        box.innerHTML = items.map((it) => `
          <span class="route-stat">
            <b class="mono" data-k="${esc(it.k)}">0</b>
            <span>${esc(it.label)}</span>
          </span>`).join('');
      }
      items.forEach((it) => {
        const el = box.querySelector('[data-k="' + it.k + '"]');
        if (el) countUp(el, vals[it.k], it.fmt);
      });
    }

    /* ---- 连线 ----
       左列智能体汇到一条总线接进代理，代理再分叉到各账号。
       当前落点那条线是实线墨色，其余是发丝虚线——一眼看出请求正走哪条。 */
    function drawLinks() {
      const box = mapWrap.getBoundingClientRect();
      if (!box.width || !box.height) return;
      svg.setAttribute('width', String(box.width));
      svg.setAttribute('height', String(box.height));
      svg.setAttribute('viewBox', '0 0 ' + box.width + ' ' + box.height);
      svg.innerHTML = '';

      const rel = (el) => {
        const r = el.getBoundingClientRect();
        return {
          left: r.left - box.left,
          right: r.right - box.left,
          mid: r.top - box.top + r.height / 2,
        };
      };
      const NS = 'http://www.w3.org/2000/svg';
      const line = (x1, y1, x2, y2, cls) => {
        const el = document.createElementNS(NS, 'path');
        el.setAttribute('d', 'M' + x1 + ',' + y1 + 'L' + x2 + ',' + y2);
        el.setAttribute('class', cls);
        svg.appendChild(el);
        return el;
      };

      const hub = mapWrap.querySelector('.route-hub-card');
      const clientEls = Array.from(mapWrap.querySelectorAll('.route-client'));
      const accEls = Array.from(mapWrap.querySelectorAll('.route-acc'));
      if (!hub || !accEls.length) return;

      const H = rel(hub);

      /* 左列 → 代理。
         左边只有一个智能体节点，所以不需要总线，但仍然走直角折线
         （横 → 竖 → 横）。两列都 align-self: center，中线本来就重合，
         多数情况下那截竖线长度是 0、整段退化成一条水平线；
         保留折线结构是为了卡片高度变化（多个 agent、备注换行）时不会塌成斜线。 */
      if (clientEls.length === 1) {
        const c = rel(clientEls[0]);
        const elbowX = c.right + 30;
        line(c.right, c.mid, elbowX, c.mid, 'rl-solid');
        line(elbowX, c.mid, elbowX, H.mid, 'rl-solid');
        line(elbowX, H.mid, H.left, H.mid, 'rl-solid');
      } else if (clientEls.length > 1) {
        const busX = Math.max(...clientEls.map((el) => rel(el).right)) + 30;
        const ys = clientEls.map((el) => rel(el).mid);
        const lo = Math.min(...ys);
        const hi = Math.max(...ys);
        const top = Math.min(lo, H.mid);
        const bot = Math.max(hi, H.mid);
        line(busX, top, busX, bot, 'rl-bus');
        clientEls.forEach((el) => {
          const c = rel(el);
          line(c.right, c.mid, busX, c.mid, 'rl-dash');
        });
        line(busX, H.mid, H.left, H.mid, 'rl-solid');
      }

      /* 代理 → 账号（扇形分叉） */
      const forkX = H.right + 34;
      line(H.right, H.mid, forkX, H.mid, 'rl-solid');

      // 代理两侧的端口：连线从这里进、从这里出，图才闭合
      const port = (cx, cy, cls) => {
        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('cx', String(cx));
        dot.setAttribute('cy', String(cy));
        dot.setAttribute('r', '2.5');
        dot.setAttribute('class', 'rl-hub-port ' + cls);
        svg.appendChild(dot);
      };
      port(H.left, H.mid, 'is-in');
      port(H.right, H.mid, 'is-out');
      const hopId = routing && routing.last_hop ? routing.last_hop.account_id : null;
      accEls.forEach((el) => {
        const a = rel(el);
        /* 候选行是两行的（名字一行、负载条一行），
           线落在名字那一行的中线上，不是整张卡的中心。 */
        const nameEl = el.querySelector('.route-acc-name');
        const target = nameEl ? rel(nameEl) : a;
        const isHop = el.dataset.acc === String(hopId);
        const usable = !el.classList.contains('is-off');
        const d = 'M' + forkX + ',' + H.mid +
          'C' + (forkX + 14) + ',' + H.mid + ' ' + (target.left - 14) + ',' + target.mid + ' ' + target.left + ',' + target.mid;
        const p = document.createElementNS(NS, 'path');
        p.setAttribute('d', d);
        p.setAttribute('class', 'rl-fork ' + (isHop ? 'is-hop' : usable ? 'is-idle' : 'is-dead'));
        svg.appendChild(p);
        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('cx', String(target.left));
        dot.setAttribute('cy', String(target.mid));
        dot.setAttribute('r', '2');
        dot.setAttribute('class', 'rl-port ' + (isHop ? 'is-hop' : usable ? 'is-idle' : 'is-dead'));
        svg.appendChild(dot);
      });
    }

    /* ---- 数据 ---- */
    async function loadRoute() {
      const [r, ag] = await Promise.all([
        api.get('/admin/routing'),
        api.get('/v1/wire/agents').catch(() => ({ agents: [] })),
      ]);
      routing = r;
      clients = ag.agents || [];
      paintHead();
      paintClients();
      paintHub();
      paintAccounts();
      paintHop();
      paintStrip();
      // 布局定下来才能量坐标，所以连线要等 DOM 稳定后再画一次
      requestAnimationFrame(drawLinks);
    }

    /* ---- 视图切换 ---- */
    async function switchTo(v) {
      mode = v;
      if (innerCleanup) { try { innerCleanup(); } catch (_) {} innerCleanup = null; }
      pane.innerHTML = '';
      // 流水视图自带窗口选择和自动刷新，顶上这个就让位给它
      autoBtn.hidden = v !== 'route';
      if (v === 'route') {
        mapWrap.innerHTML = `
          <div class="route-head">
            <div class="route-head-tabs"></div>
            <p class="route-head-note"></p>
          </div>
          <section class="route-col route-clients">
            <div class="route-col-head"><span class="route-col-title">智能体</span>
              <span class="micro" data-role="client-head"></span></div>
            <div class="route-clients-list"></div>
          </section>
          <section class="route-col route-hub">
            <div class="route-hub-card">
              <div class="route-hub-mark" aria-hidden="true">⬢</div>
              <div class="route-hub-name">MARVIS PROXY</div>
              <div class="route-hub-addr mono"></div>
              <span class="route-hub-tag"></span>
              <span class="route-hub-badge">待命</span>
            </div>
            <div class="route-hub-note"></div>
          </section>
          <section class="route-col route-accounts">
            <div class="route-col-head"><span class="route-col-title">候选账号</span>
              <span class="micro" data-role="acc-head"></span></div>
            <div class="route-accs"></div>
          </section>`;
        mapWrap.appendChild(svg);

        // 顶部页签：切的是「看判定链第几步」
        const chainSeg = segmented(
          CHAIN.map((c) => ({ v: c.v, label: c.label })),
          step,
          (v2) => {
            step = v2;
            chainSeg.setValue(v2);
            paintHead();
            paintAccounts();
            requestAnimationFrame(drawLinks);
          }
        );
        const tabs = mapWrap.querySelector('.route-head-tabs');
        tabs.appendChild(chainSeg.node);

        pane.appendChild(mapWrap);
        pane.appendChild(footBox);
        await loadRoute();
        requestAnimationFrame(drawLinks);
      } else {
        const r = await flowPage.render();
        pane.appendChild(r.node);
        if (r.cleanup) innerCleanup = r.cleanup;
      }
    }

    autoBtn.addEventListener('click', () => {
      const on = autoBtn.textContent.indexOf('开') < 0;
      autoBtn.textContent = '自动刷新：' + (on ? '开' : '关');
      autoBtn.classList.toggle('btn-primary', on);
      if (on) {
        timer = setInterval(() => {
          if (mode === 'route') loadRoute().catch(toastError);
        }, 4000);
        toast('每 4 秒拉一次', 'good');
      } else if (timer) {
        clearInterval(timer);
        timer = null;
      }
    });

    await switchTo('route');

    // 窗口尺寸变了连线要重算（列宽一变，扇形的曲率就变了）
    let raf = 0;
    const ro = window.ResizeObserver
      ? new ResizeObserver(() => {
          cancelAnimationFrame(raf);
          raf = requestAnimationFrame(drawLinks);
        })
      : null;
    if (ro) ro.observe(mapWrap);
    window.addEventListener('resize', drawLinks);

    return {
      node: root,
      cleanup: () => {
        if (timer) clearInterval(timer);
        if (innerCleanup) { try { innerCleanup(); } catch (_) {} }
        if (ro) ro.disconnect();
        window.removeEventListener('resize', drawLinks);
      },
    };
  },
};
