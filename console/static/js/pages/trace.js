/* ============================================================
   pages/trace.js —— 请求流水
   最近一轮请求的摘要。不含票据、不含 prompt 正文、不含凭证明文。

   现在是「路由」页的第二个视图（见 pages/routing.js）：
   路由图回答「此刻会送到谁」，这里回答「刚才送到了谁」。
   ============================================================ */

import {
  api, h, esc, fmtInt, fmtCompact, fmtMs, fmtClock,
  toast, toastError, ledTag, segmented,
} from '../core.js';

const WINDOWS = [
  { v: 15, label: '15 分钟' },
  { v: 60, label: '1 小时' },
  { v: 180, label: '3 小时' },
  { v: 1440, label: '24 小时' },
];

export default {
  sub: '每轮请求走过哪个模型、哪个槽位、哪个号、花了多久。表格按时间倒序。',

  async render() {
    const root = h(`<div class="stack"></div>`);

    let minutes = 60;
    let limit = 60;
    let auto = false;
    let timer = null;
    let lastSeen = null;   // 已展示过的最新 id
    let firstPaint = true;
    let filter = '';       // 过滤文本
    let onlyErrors = false;

    const strip = h(`<div class="strip">
      <div class="readout"><span class="readout-label">窗口内请求</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">轮</span></span>
        <span class="readout-note" data-note="n">—</span></div>
      <div class="readout"><span class="readout-label">失败</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">轮</span></span>
        <span class="readout-note" data-note="err">—</span></div>
      <div class="readout"><span class="readout-label">流式占比</span>
        <span class="readout-value">—</span>
        <span class="readout-note" data-note="st">—</span></div>
      <div class="readout"><span class="readout-label">最慢一轮</span>
        <span class="readout-value">—</span>
        <span class="readout-note" data-note="slow">—</span></div>
    </div>`);

    const tableWrap = h(`<div class="tbl-wrap"></div>`);
    const metaEl = h(`<span class="shead-meta"></span>`);

    /* ---- 控制条 ---- */
    const winSeg = segmented(
      WINDOWS.map((w) => ({ v: w.v, label: w.label })),
      minutes,
      (v) => { minutes = Number(v); load(true); }
    );

    const autoBtn = h(`<button class="btn btn-ghost push-right" type="button">自动刷新：关</button>`);

    /* ---- 过滤：Magpie 的模型选择器靠一个搜索框收敛 45 个 Agent，
       这里同理——一个窗口几十上百行，没搜索就没法定位。 ---- */
    const filterInput = h(`<input class="input search-input" type="search"
      placeholder="过滤：模型 / 槽位 / 账号 / 状态码" autocomplete="off">`);
    const errBtn = h(`<button class="btn btn-ghost" type="button">只看失败</button>`);

    filterInput.addEventListener('input', () => {
      filter = filterInput.value.trim().toLowerCase();
      applyFilter();
    });
    errBtn.addEventListener('click', () => {
      onlyErrors = !onlyErrors;
      errBtn.classList.toggle('btn-primary', onlyErrors);
      errBtn.textContent = onlyErrors ? '只看失败 · 开' : '只看失败';
      applyFilter();
    });

    function matches(i) {
      if (onlyErrors && Number(i.status) < 400) return false;
      if (!filter) return true;
      return [i.model, i.slot, i.account, i.kind, i.status]
        .some((v) => String(v == null ? '' : v).toLowerCase().includes(filter));
    }

    let rows = [];      // 最近一次拉到的全部行
    let rowByKey = new Map();

    /* trace 的条目没有 id 字段，唯一能当主键的是 at 时间戳。
       同一毫秒内不会有两轮请求，够用。 */
    function keyOf(i) {
      return String(i.at);
    }

    function applyFilter() {
      const body = tableWrap.querySelector('tbody');
      if (!body) return;
      let n = 0;
      body.querySelectorAll('tr').forEach((tr) => {
        const rec = rowByKey.get(tr.dataset.key);
        const hit = rec ? matches(rec) : false;
        tr.hidden = !hit;
        if (hit) n++;
      });
      const meta = document.getElementById('trace-filter-meta');
      if (meta) {
        meta.textContent = (filter || onlyErrors)
          ? `筛出 ${n} / ${rows.length} 轮`
          : '';
      }
    }

    autoBtn.addEventListener('click', () => {
      auto = !auto;
      autoBtn.textContent = '自动刷新：' + (auto ? '开' : '关');
      autoBtn.classList.toggle('btn-primary', auto);
      if (auto) {
        timer = setInterval(() => load(false), 4000);
        toast('每 4 秒拉一次', 'good');
      } else if (timer) {
        clearInterval(timer);
        timer = null;
      }
    });

    /* ---- 加载 ---- */
    async function load(reset) {
      if (reset) { firstPaint = true; lastSeen = null; }
      try {
        const r = await api.get(`/admin/trace?limit=${limit}&minutes=${minutes}`);
        const items = r.items || [];

        const failed = items.filter((i) => Number(i.status) >= 400).length;
        const streamed = items.filter((i) => i.stream === 1).length;
        const slowest = items.reduce((m, i) => Math.max(m, Number(i.ms) || 0), 0);

        const cells = strip.querySelectorAll('.rv-main');
        cells[0].textContent = fmtInt(items.length);
        cells[1].textContent = fmtInt(failed);
        strip.children[1].classList.toggle('is-alert', failed > 0);
        strip.children[2].querySelector('.readout-value').textContent =
          items.length ? Math.round((streamed / items.length) * 100) + '%' : '—';
        strip.children[3].querySelector('.readout-value').textContent = fmtMs(slowest);

        const notes = strip.querySelectorAll('[data-note]');
        const avg = items.length
          ? Math.round(items.reduce((s, i) => s + (Number(i.ms) || 0), 0) / items.length) : 0;
        notes[0].textContent = `最近 ${minutes} 分钟 · 最多 ${limit} 轮`;
        notes[1].textContent = items.length
          ? `占请求 ${Math.round((failed / items.length) * 100)}%`
          : '—';
        notes[2].textContent = items.length ? `${fmtInt(streamed)} 轮流式` : '—';
        notes[3].textContent = items.length ? `平均 ${fmtMs(avg)}` : '—';

        metaEl.textContent = `最近 ${items.length} 轮 · ${minutes} 分钟窗口 · ${r.upstream || ''}`;

        if (!items.length) {
          tableWrap.innerHTML = `<div class="empty">
            <div class="empty-title">这个窗口里没有请求</div>
            <div class="empty-text">让客户端跑一轮 <code>/v1/chat/completions</code>，这里马上就有东西。</div>
          </div>`;
          return;
        }

        // 新到的高亮：接口不返回 id，只能拿时间戳比。
        const maxAt = items.reduce((m, i) => Math.max(m, Number(i.at) || 0), 0);

        rows = items;
        rowByKey = new Map(items.map((i) => [keyOf(i), i]));

        tableWrap.innerHTML = `
          <table class="tbl">
            <thead><tr>
              <th>时间</th><th>模型</th><th>槽位</th><th>账号</th>
              <th>状态</th><th class="r">耗时</th>
              <th class="r">提示</th><th class="r">生成</th><th class="r">推理</th>
              <th>流式</th><th>类型</th>
            </tr></thead>
            <tbody>
              ${items.map((i) => {
                const st = Number(i.status) || 0;
                // 200 是常态，不用徽章——满屏一样的绿点反而看不见异常。
                const statusCell = st < 400
                  ? `<span class="cell-mute mono">${esc(st)}</span>`
                  : ledTag(String(st), st >= 500 ? 'bad' : 'warn');
                const fresh = !firstPaint && lastSeen !== null && Number(i.at) > lastSeen;
                return `<tr data-key="${esc(keyOf(i))}" class="${fresh ? 'row-fresh' : ''}${st >= 400 ? ' is-hot' : ''}">
                  <td class="nowrap mono">${esc(fmtClock(i.at))}</td>
                  <td class="nowrap mono cell-strong">${esc(i.model || '—')}</td>
                  <td class="nowrap mono cell-mute">${esc(i.slot || '—')}</td>
                  <td class="nowrap cell-mute">${esc(i.account || '—')}</td>
                  <td>${statusCell}</td>
                  <td class="r">${esc(fmtMs(i.ms))}</td>
                  <td class="r">${esc(fmtCompact(i.prompt_tokens))}</td>
                  <td class="r">${esc(fmtCompact(i.completion_tokens))}</td>
                  <td class="r cell-mute">${esc(fmtCompact(i.reasoning_tokens))}</td>
                  <td class="cell-mute">${i.stream ? '是' : '否'}</td>
                  <td class="cell-mute nowrap">${esc(i.kind || '—')}</td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>`;

        applyFilter();
        lastSeen = maxAt;
        firstPaint = false;
      } catch (e) {
        tableWrap.innerHTML = `<div class="notice is-bad" style="margin:14px">${esc(e.message)}</div>`;
        toastError(e);
      }
    }

    const tools = h(`<div class="toolbar"></div>`);
    tools.appendChild(winSeg.node);
    tools.appendChild(autoBtn);

    const filterBar = h(`<div class="toolbar">
      <div class="search-wrap">${''}</div>
      <span class="shead-meta" id="trace-filter-meta"></span>
    </div>`);
    filterBar.querySelector('.search-wrap').appendChild(filterInput);
    filterBar.appendChild(errBtn);

    root.appendChild(strip);
    root.appendChild(tools);
    root.appendChild(filterBar);
    root.appendChild(h(`<div class="shead">
      <span class="shead-label">请求流水</span><span class="shead-rule"></span>
    </div>`));
    root.appendChild(tableWrap);

    root.appendChild(h(`<div class="notice">
      这一页只给摘要：不含票据（uskey）、不含 prompt 正文、不含 openid / access_token。
      账号列显示的是备注，不是原始 ID。
    </div>`));

    await load(true);

    return {
      node: root,
      cleanup: () => { if (timer) clearInterval(timer); },
    };
  },
};
