/* ============================================================
   pages/models.js —— 模型
   /v1/models 是客户端真正看到的那份清单。
   ============================================================ */

import {
  api, h, esc, fmtInt, fmtCompact, countUp,
} from '../core.js';

const CAP_LABEL = { text: '文本', vision: '视觉', image: '出图', tools: '工具', reasoning: '推理' };

export default {
  sub: '对外暴露的模型清单。多个名字可能落在同一个上游槽位上——那是刻意的路由，不是重复。',

  async render() {
    const root = h(`<div class="stack"></div>`);

    let payload;
    try {
      payload = await api.get('/v1/models');
    } catch (e) {
      return { node: h(`<div class="notice is-bad">读不到 /v1/models：${esc(e.message)}</div>`) };
    }

    const models = payload.data || [];
    const slots = new Set(models.map((m) => m.marvis_slot).filter(Boolean));
    const maxCtx = models.reduce((s, m) => Math.max(s, m.context_window || 0), 0);
    const maxOut = models.reduce((s, m) => Math.max(s, m.max_output_tokens || 0), 0);
    const capSum = models.reduce((s, m) => s + Object.values(m.capabilities || {}).filter(Boolean).length, 0);

    const strip = h(`<div class="strip">
      <div class="readout"><span class="readout-label">模型</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">个</span></span>
        <span class="readout-note">客户端看到的别名数量</span></div>
      <div class="readout"><span class="readout-label">上游槽位</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">个</span></span>
        <span class="readout-note">真正的路由目的地</span></div>
      <div class="readout"><span class="readout-label">最大上下文</span>
        <span class="readout-value"><span class="rv-main">0</span></span>
        <span class="readout-note">按厂商公开规格填</span></div>
      <div class="readout"><span class="readout-label">单次输出上限</span>
        <span class="readout-value"><span class="rv-main">0</span></span>
        <span class="readout-note">单次响应最多 token</span></div>
    </div>`);

    const cells = strip.querySelectorAll('.rv-main');
    countUp(cells[0], models.length, fmtInt);
    countUp(cells[1], slots.size, fmtInt);
    countUp(cells[2], maxCtx, fmtCompact);
    countUp(cells[3], maxOut, fmtCompact);

    /* ---- 槽位归属 ---- */
    const bySlot = new Map();
    models.forEach((m) => {
      const s = m.marvis_slot || '—';
      if (!bySlot.has(s)) bySlot.set(s, []);
      bySlot.get(s).push(m);
    });

    // Magpie 把同一个 provider 下的模型收在一组，右上角挂个总数。
    // 这里同理：一个槽位 = 一组别名，头部右边写「N 个别名」。
    const slotList = h(`<div class="stack" style="gap:1px;background:var(--rule-soft);border:1px solid var(--rule)">
      ${Array.from(bySlot.entries()).map(([slot, list]) => `
        <div class="slot">
          <div class="slot-head">
            <span class="slot-name">${esc(slot)}</span>
            <span class="micro">${list.length} 个别名</span>
          </div>
          <div class="row-tight">
            ${list.map((m) => `<span class="tag tag-idle">${esc(m.id)}</span>`).join('')}
          </div>
        </div>`).join('')}
    </div>`);

    /* ---- 模型表 ----
       别处（Magpie 的模型选择器）不铺一张宽表，而是「主名 + 副行」。
       这里照做：列少了才装得下，说明文字才有地方待。 */
    const table = h(`<div class="tbl-wrap">
      <table class="tbl">
        <thead><tr>
          <th style="width:280px">模型</th><th style="width:170px">槽位</th>
          <th style="width:196px">能力</th>
          <th class="r" style="width:78px">上下文</th><th class="r" style="width:86px">输出上限</th>
          <th style="width:210px">别名</th>
        </tr></thead>
        <tbody>
          ${models.map((m) => {
            const caps = m.capabilities || {};
            const capHtml = Object.keys(CAP_LABEL)
              .filter((k) => caps[k])
              .map((k) => `<span class="tag tag-ok">${esc(CAP_LABEL[k])}</span>`).join('');
            const alts = (m.alt_ids || []).filter(Boolean);
            const dn = m.display_name || m.name || '';
            const desc = m.description || '';
            return `<tr>
              <td>
                <div class="cell-strong mono nowrap">${esc(m.id)}</div>
                ${dn ? `<div class="cell-note" title="${esc(desc)}">${esc(dn)}</div>` : ''}
              </td>
              <td class="nowrap mono cell-mute">${esc(m.marvis_slot || '—')}</td>
              <td class="cell-caps">${capHtml || '<span class="cell-mute">—</span>'}</td>
              <td class="r">${esc(fmtCompact(m.context_window))}</td>
              <td class="r">${esc(fmtCompact(m.max_output_tokens))}</td>
              <td>
                ${alts.length
                  ? `<div class="row-tight">${alts.map((a) =>
                      `<span class="tag tag-idle" title="${esc(a)}">${esc(a)}</span>`).join('')}</div>`
                  : '<span class="cell-mute">—</span>'}
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`);

    root.appendChild(strip);
    root.appendChild(h(`<div class="shead">
      <span class="shead-label">槽位归属</span><span class="shead-rule"></span>
      <span class="shead-meta">同一槽位下的别名落到同一个真实模型</span>
    </div>`));
    root.appendChild(slotList);
    root.appendChild(h(`<div class="shead">
      <span class="shead-label">模型清单</span><span class="shead-rule"></span>
      <span class="shead-meta">GET /v1/models</span>
    </div>`));
    root.appendChild(table);

    return { node: root };
  },
};
