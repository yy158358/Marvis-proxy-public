/* ============================================================
   pages/usage.js —— 用量
   请求量、token 构成、延迟分位，按天 / 模型 / 账号切。
   ============================================================ */

import {
  api, h, esc, fmtInt, fmtCompact, fmtMs, fmtPct,
  bars, tokenBar, countUp, segmented, toastError,
} from '../core.js';

const DAYS = [
  { v: 1, label: '今天' },
  { v: 7, label: '7 天' },
  { v: 30, label: '30 天' },
  { v: 90, label: '90 天' },
];

const BY = [
  { v: 'day', label: '按天' },
  { v: 'model', label: '按模型' },
  { v: 'account', label: '按账号' },
];

export default {
  sub: '数据直接来自 usage 表聚合。token 分三段：提示、生成、推理。',

  async render() {
    const root = h(`<div class="stack"></div>`);
    let days = 7;
    let by = 'day';

    const strip = h(`<div class="strip">
      <div class="readout"><span class="readout-label">请求</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">次</span></span>
        <span class="readout-note" data-note="req">—</span></div>
      <div class="readout"><span class="readout-label">Token</span>
        <span class="readout-value"><span class="rv-main">0</span></span>
        <span class="readout-note" data-note="tok">—</span></div>
      <div class="readout"><span class="readout-label">平均延迟</span>
        <span class="readout-value"><span class="rv-main">—</span></span>
        <span class="readout-note" data-note="avg">—</span></div>
      <div class="readout"><span class="readout-label">最慢 p95</span>
        <span class="readout-value"><span class="rv-main">—</span></span>
        <span class="readout-note" data-note="p95">—</span></div>
      <div class="readout"><span class="readout-label">失败</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">次</span></span>
        <span class="readout-note" data-note="err">—</span></div>
      <div class="readout"><span class="readout-label">限流</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">次</span></span>
        <span class="readout-note" data-note="thr">—</span></div>
    </div>`);

    const chartWell = h(`<div class="well well-grid"></div>`);
    const tableWrap = h(`<div class="tbl-wrap"></div>`);

    let lastItems = [];

    /** 图表按容器真实像素宽画——这样坐标文字不会被横向拉变形。 */
    function drawChart() {
      const w = Math.max(320, Math.round(chartWell.clientWidth) - 8);
      const chartItems = (lastItems || []).map((it) => ({
        label: it.bucket,
        xLabel: by === 'day' ? String(it.bucket).slice(5) : '',
        value: it.requests || 0,
      }));
      const p95line = (lastItems || []).map((it) => it.p95_ms || 0);
      chartWell.innerHTML = `
        ${bars(chartItems, {
          width: w, height: 230, label: '次', showX: by === 'day',
          line: p95line, lineColor: '#C43F27', lineLabel: 'p95',
        })}
        <div class="row" style="padding:10px 12px 8px;justify-content:space-between">
          <div class="legend">
            <span class="legend-item"><i class="legend-key" style="background:#8B8477"></i>请求数（左轴）</span>
            <span class="legend-item"><i class="legend-key" style="background:#C43F27"></i>p95 延迟（右轴）</span>
          </div>
          <span class="micro">${by === 'day' ? '按天' : by === 'model' ? '按模型' : '按账号'}</span>
        </div>`;
    }

    const daySeg = segmented(
      DAYS.map((d) => ({ v: d.v, label: d.label })),
      days,
      (v) => { days = Number(v); load(); }
    );

    const bySeg = segmented(
      BY.map((b) => ({ v: b.v, label: b.label })),
      by,
      (v) => { by = v; load(); }
    );

    async function load() {
      chartWell.classList.add('is-loading');
      tableWrap.classList.add('is-loading');
      try {
        const r = await api.get(`/admin/usage?days=${days}&by=${by}`);
        const items = r.items || [];

        const sum = items.reduce((s, it) => {
          s.requests += it.requests || 0;
          s.prompt += it.prompt_tokens || 0;
          s.completion += it.completion_tokens || 0;
          s.reasoning += it.reasoning_tokens || 0;
          s.total += it.total_tokens || 0;
          s.errors += it.errors || 0;
          s.throttled += it.throttled || 0;
          s.streaming += it.streaming || 0;
          s.avgSum += (it.avg_ms || 0) * (it.requests || 0);
          s.p95 = Math.max(s.p95, it.p95_ms || 0);
          return s;
        }, { requests: 0, prompt: 0, completion: 0, reasoning: 0, total: 0, errors: 0, throttled: 0, streaming: 0, avgSum: 0, p95: 0 });

        const avg = sum.requests ? Math.round(sum.avgSum / sum.requests) : 0;

        // 六个读数都有 .rv-main，顺序与 strip 一致。
        // 计数类滚动；延迟类直接落值——中途换单位会闪。
        const cells = strip.querySelectorAll('.rv-main');
        countUp(cells[0], sum.requests, fmtCompact);
        countUp(cells[1], sum.total, fmtCompact);
        cells[2].textContent = fmtMs(avg);
        cells[3].textContent = fmtMs(sum.p95);
        countUp(cells[4], sum.errors, fmtInt);
        countUp(cells[5], sum.throttled, fmtInt);
        strip.children[4].classList.toggle('is-alert', sum.errors > 0);
        strip.children[5].classList.toggle('is-alert', sum.throttled > 0);

        // 每个读数下面挂一句能拆开看的补充——
        // 光有总数没用，得知道它由什么构成。
        const notes = strip.querySelectorAll('[data-note]');
        const streamingPct = sum.requests ? Math.round((sum.streaming / sum.requests) * 100) : 0;
        const errPct = sum.requests ? (sum.errors / sum.requests) * 100 : 0;
        notes[0].textContent = sum.requests
          ? `流式 ${fmtInt(sum.streaming)} 次 · ${streamingPct}%`
          : '这个区间没有记录';
        notes[1].textContent = sum.total
          ? `提示 ${fmtCompact(sum.prompt)} · 生成 ${fmtCompact(sum.completion)}`
          : '—';
        notes[2].textContent = sum.requests ? `每轮平均耗时` : '—';
        notes[3].textContent = sum.p95 ? `慢于 p95 的请求会被重试` : '—';
        notes[4].textContent = sum.requests ? `占请求 ${fmtPct(errPct)}` : '—';
        notes[5].textContent = sum.requests ? `占请求 ${fmtPct(sum.requests ? (sum.throttled / sum.requests) * 100 : 0)}` : '—';

        // 工具条右端的区间合计
        countUp(agg.querySelector('.rv-main'), sum.requests, fmtInt);

        // 图：请求量柱（纯色柱高 = 请求数）。token 构成不看这张图，看下面表格里的 tokenBar。
        lastItems = items;
        drawChart();

        if (!items.length) {
          tableWrap.innerHTML = `<div class="empty">
            <div class="empty-title">这个区间没有记录</div>
            <div class="empty-text">换个时间窗，或者让上游先跑几轮。</div>
          </div>`;
          return;
        }

        tableWrap.innerHTML = `
          <table class="tbl">
            <thead><tr>
              <th>${by === 'day' ? '日期' : by === 'model' ? '模型' : '账号'}</th>
              <th class="r">请求</th><th class="r">流式</th>
              <th class="r">提示</th><th class="r">生成</th><th class="r">推理</th>
              <th style="width:180px">构成</th>              <th class="r">平均</th><th class="r">p95</th><th class="r">失败</th><th class="r">限流</th>
            </tr></thead>
            <tbody>
              ${items.map((it) => `
                <tr>
                  <td class="cell-strong nowrap">${esc(it.bucket)}</td>
                  <td class="r">${esc(fmtInt(it.requests))}</td>
                  <td class="r cell-mute">${esc(fmtInt(it.streaming))}</td>
                  <td class="r">${esc(fmtCompact(it.prompt_tokens))}</td>
                  <td class="r">${esc(fmtCompact(it.completion_tokens))}</td>
                  <td class="r cell-mute">${esc(fmtCompact(it.reasoning_tokens))}</td>
                  <td>${tokenBar(it.prompt_tokens, it.completion_tokens, it.reasoning_tokens)}</td>
                  <td class="r">${esc(fmtMs(it.avg_ms))}</td>
                  <td class="r">${esc(fmtMs(it.p95_ms))}</td>
                  <td class="r ${it.errors ? '' : 'cell-mute'}">${esc(fmtInt(it.errors))}</td>
                  <td class="r ${it.throttled ? '' : 'cell-mute'}">${esc(fmtInt(it.throttled))}</td>
                </tr>`).join('')}
            </tbody>
          </table>`;
      } catch (e) {
        chartWell.innerHTML = `<div class="notice is-bad" style="margin:14px">${esc(e.message)}</div>`;
        tableWrap.innerHTML = '';
        toastError(e);
      } finally {
        chartWell.classList.remove('is-loading');
        tableWrap.classList.remove('is-loading');
      }
    }

    const chartHead = h(`<div class="shead">
      <span class="shead-label">分布</span><span class="shead-rule"></span>
      <span class="shead-meta">柱高为请求数</span>
    </div>`);

    const tableHead = h(`<div class="shead">
      <span class="shead-label">明细</span><span class="shead-rule"></span>
      <span class="shead-meta" id="usage-meta"></span>
    </div>`);

    const tokenLegend = h(`<div class="row" style="justify-content:flex-end">
      <div class="legend">
        <span class="legend-item"><i class="legend-key" style="background:#8B8477"></i>提示</span>
        <span class="legend-item"><i class="legend-key" style="background:#12696E"></i>生成</span>
        <span class="legend-item"><i class="legend-key" style="background:#C43F27"></i>推理</span>
      </div>
    </div>`);

    const tools = h(`<div class="toolbar"></div>`);
    tools.appendChild(daySeg.node);
    tools.appendChild(bySeg.node);

    // Magpie 那一行的右边放的是「这一段的总量」。用量页没有价格，
    // 但「区间合计」是同一个作用：一眼知道这段时间烧了多少。
    const agg = h(`<div class="agg">
      <span class="agg-label">区间合计</span>
      <span class="agg-value"><span class="rv-main">0</span><span class="agg-unit">次请求</span></span>
    </div>`);
    tools.appendChild(agg);

    root.appendChild(strip);
    root.appendChild(tools);
    root.appendChild(chartHead);
    root.appendChild(chartWell);
    root.appendChild(tableHead);
    root.appendChild(tokenLegend);
    root.appendChild(tableWrap);

    await load();
    return {
      node: root,
      onMount: () => { drawChart(); },
    };
  },
};
