/* ============================================================
   pages/overview.js —— 概览
   一屏之内回答三件事：链路活着吗、账号还有余量吗、刚才慢在哪儿。
   ============================================================ */

import {
  api, h, esc, fmtCompact, fmtInt, fmtMs, fmtPct,
  waveform, quantile, countUp, ledTag, toast, toastError,
} from '../core.js';

async function safe(p) {
  try { return await p; } catch (e) { console.warn(e); return null; }
}

export default {
  sub: '链路是否健康、账号池还剩多少余量、最近的请求慢在哪儿。',

  async render() {
    const [health, acc, use, tr, wallet, agents] = await Promise.all([
      safe(api.get('/health')),
      safe(api.get('/admin/accounts')),
      safe(api.get('/admin/usage?days=1&by=day')),
      safe(api.get('/admin/trace?limit=60&minutes=180')),
      safe(api.get('/admin/wallet')),
      safe(api.get('/v1/wire/agents')),
    ]);

    if (!health) {
      return { node: h(`<div class="notice is-bad"><strong>读不到 /health。</strong><br>服务可能没起来，或者令牌不对。</div>`) };
    }

    const counts = acc && acc.counts ? acc.counts : { total: 0, active: 0, cooling: 0, expired: 0 };
    const today = use && use.items && use.items.length ? use.items[use.items.length - 1] : null;
    const reqs = today ? today.requests : 0;
    const tokens = today ? today.total_tokens : 0;
    const errors = today ? today.errors : 0;
    const p95 = today ? today.p95_ms : 0;
    const errRate = reqs ? (errors / reqs) * 100 : 0;

    const items = tr && tr.items ? tr.items.slice().reverse() : [];
    const lat = items.map((i) => Number(i.ms) || 0).filter((v) => v > 0);
    const sorted = lat.slice().sort((a, b) => a - b);
    const med = quantile(sorted, 0.5);

    const root = h(`<div class="stack"></div>`);

    /* ---- 告警：状态变化必须看得见，不能只躺在日志里 ----
       Magpie 的核心主张之一：每一次降级都能被查到，而不是默默发生。
       这里同理——号失效、临近到期、池子见底，都要在第一屏喊出来。 */
    const accList = acc && acc.accounts ? acc.accounts : [];
    const alerts = [];
    const expiredList = accList.filter((a) => a.status === 'expired');
    const coolingList = accList.filter((a) => a.cooldown > 0);
    const expiringList = accList.filter((a) => a.days_left !== null && a.days_left >= 0 && a.days_left < 4);

    if (expiredList.length) {
      alerts.push({
        kind: 'bad',
        text: `${expiredList.length} 个号已失效，不再参与调度。`,
        detail: expiredList.map((a) => a.openid).join('、'),
      });
    }
    if (expiringList.length) {
      alerts.push({
        kind: 'warn',
        text: `${expiringList.length} 个号 4 天内到期。`,
        detail: expiringList.map((a) => `${a.openid}（余 ${a.days_left} 天）`).join('、'),
      });
    }
    if (coolingList.length) {
      alerts.push({
        kind: 'warn',
        text: `${coolingList.length} 个号正在冷却，等退避结束。`,
        detail: coolingList.map((a) => `${a.openid}（${a.cooldown}s）`).join('、'),
      });
    }
    if (counts.total > 0 && counts.active === 0) {
      alerts.push({
        kind: 'bad',
        text: '账号池里没有一个在岗的号，所有请求都会失败。',
        detail: '去「账号池」加号或启用被停用的账号。',
      });
    }

    const alertBox = h(`
      <div class="alerts">
        ${alerts.map((a) => `
          <div class="notice is-${a.kind}">
            <strong>${esc(a.text)}</strong>
            <span class="alert-detail">${esc(a.detail)}</span>
          </div>`).join('')}
      </div>`);

    /* ---- 读数带 ---- */
    const strip = h(`
      <div class="strip">
        <div class="readout ${counts.expired ? 'is-alert' : 'is-good'}">
          <span class="readout-label">账号池</span>
          <span class="readout-value" data-count="${counts.active}"><span class="rv-main">0</span><span class="readout-unit">/ ${counts.total} 可用</span></span>
          <span class="readout-bar ${counts.expired ? 'is-alert' : 'is-good'}"><i style="width:${counts.total ? (counts.active / counts.total * 100).toFixed(1) : 0}%"></i></span>
          <span class="readout-note">${counts.cooling} 冷却中 · ${counts.expired} 已失效</span>
        </div>

        <div class="readout">
          <span class="readout-label">今日请求</span>
          <span class="readout-value" data-count="${reqs}"><span class="rv-main">0</span><span class="readout-unit">次</span></span>
          <span class="readout-note">${today ? '平均 ' + fmtMs(today.avg_ms) + ' · 流式 ' + fmtInt(today.streaming) + ' 次' : '今天还没有记录'}</span>
        </div>

        <div class="readout">
          <span class="readout-label">今日 Token</span>
          <span class="readout-value" data-count="${tokens}"><span class="rv-main">0</span></span>
          <span class="readout-note">${today ? '提示 ' + fmtCompact(today.prompt_tokens) + ' · 生成 ' + fmtCompact(today.completion_tokens) : '—'}</span>
        </div>

        <div class="readout ${p95 > 8000 ? 'is-alert' : ''}">
          <span class="readout-label">p95 延迟</span>
          <span class="readout-value">${fmtMs(p95)}</span>
          <span class="readout-note">中位 ${fmtMs(med || (today ? today.avg_ms : 0))}</span>
        </div>

        <div class="readout ${errRate > 3 ? 'is-alert' : ''}">
          <span class="readout-label">错误率</span>
          <span class="readout-value">${fmtPct(errRate)}</span>
          <span class="readout-note">${errors} 次失败${today && today.throttled ? ' · ' + today.throttled + ' 次限流' : ''}</span>
        </div>
      </div>`);

    // 数字滚动
    strip.querySelectorAll('[data-count]').forEach((el) => {
      const to = Number(el.dataset.count) || 0;
      const main = el.querySelector('.rv-main');
      countUp(main, to, fmtCompact);
    });

    /* ---- 波形 ---- */
    const waveBox = h(`<div class="well well-grid"></div>`);

    /** 波形按容器真实像素宽画，标注文字才不会被横向拉变形。 */
    function drawWave() {
      const w = Math.max(320, Math.round(waveBox.clientWidth) - 8);
      waveBox.innerHTML = waveform(lat, {
        width: w, height: 200,
        p95: sorted.length ? quantile(sorted, 0.95) : 0,
        median: med,
      });
    }
    drawWave();

    const waveHead = h(`
      <div class="shead">
        <span class="shead-label">延迟波形</span>
        <span class="shead-rule"></span>
        <span class="shead-meta">最近 ${lat.length} 轮 · 近 3 小时</span>
      </div>`);

    /* ---- 状态台账 ---- */
    const bind = health.bind || {};
    const uskey = health.uskey || {};
    const chans = uskey.channels || {};
    const chanHtml = ['ctypes', 'pure', 'node', 'local'].map((c) => {
      const ok = chans[c] === true;
      const err = uskey[c + '_err'];
      const label = { ctypes: 'DLL 通道', pure: '纯 Python', node: 'Node', local: '本地' }[c] || c;
      return `<span class="tag ${ok ? 'tag-ok' : 'tag-bad'}" title="${esc(err || (ok ? '可用' : '不可用'))}">
        <i class="led ${ok ? 'fill-ok' : 'fill-bad'}"></i>${esc(label)}</span>`;
    }).join('');

    const wiredAgents = agents && agents.agents ? agents.agents.filter((a) => a.wired) : [];
    const installedAgents = agents && agents.agents ? agents.agents.filter((a) => a.installed) : [];

    const ledgerRows = [
      {
        k: '监听地址',
        v: `<span class="mono">${esc(bind.host || '?')}:${esc(bind.port || '?')}</span>`,
        flag: bind.lan_reachable === false,
        note: bind.lan_reachable === false
          ? '只在本机可达，手机 / 局域网连不上。要外网访问得把 config.json 的 host 改成 0.0.0.0。'
          : '局域网可直达。',
      },
      {
        k: '出票通道',
        v: chanHtml,
        flag: !uskey.ok,
        note: uskey.ok
          ? `当前后端 ${esc(uskey.backend || '—')}${uskey.app_ver ? ' · 客户端版本 ' + esc(uskey.app_ver) : ''}`
          : (uskey.error || '出票通道不可用，上游会 403。'),
      },
      {
        k: '钱包',
        v: wallet
          ? `<span class="mono">${esc(fmtCompact(wallet.used))} / ${esc(fmtCompact(wallet.total))}</span>
             <span class="muted small">（余 ${esc(fmtPct(100 - (wallet.percent || 0)))}）</span>`
          : '<span class="muted">读不到</span>',
        flag: !!(wallet && (wallet.percent || 0) > 90),
        note: wallet ? `状态码 ${esc(wallet.wallet_status)}` : '',
      },
      {
        k: '接入工具',
        v: installedAgents.length
          ? installedAgents.map((a) => `<span class="tag ${a.wired ? 'tag-ok' : 'tag-idle'}">
              <i class="led ${a.wired ? 'fill-ok' : 'fill-off'}"></i>${esc(a.display_name)}</span>`).join('')
          : '<span class="muted">没检测到</span>',
        note: `${wiredAgents.length} / ${installedAgents.length} 已指向本代理`,
      },
      {
        k: '在途请求',
        v: `<span class="mono">${esc(fmtInt(health.inflight || 0))}</span>`,
        note: `并发上限 ${esc(health.account_max_concurrency || '—')}`,
      },
      {
        k: '运行中任务',
        v: `<span class="mono">${esc(fmtInt(health.tasks_running || 0))}</span>`,
        note: '图像生成等异步任务',
      },
      {
        k: '最近清理',
        v: health.last_cleanup
          ? `<span class="mono">${esc(health.last_cleanup.at || '')}</span>`
          : '<span class="muted">尚未跑过</span>',
        note: health.last_cleanup
          ? `删文件 ${health.last_cleanup.removed_files} · 删任务 ${health.last_cleanup.removed_tasks} · 删用量 ${health.last_cleanup.removed_usage}`
          : '',
      },
    ];

    const ledger = h(`
      <div class="ledger">
        ${ledgerRows.map((r) => `
          <div class="ledger-row${r.flag ? ' is-flagged' : ''}">
            <dt>${esc(r.k)}</dt>
            <dd>${r.v}${r.note ? `<div class="small muted" style="margin-top:3px">${esc(r.note)}</div>` : ''}</dd>
          </div>`).join('')}
      </div>`);

    /* ---- 账号余量 ---- */
    const accRows = accList;
    const gauges = h(`
      <div class="gauges">
        ${accRows.map((a) => {
          const cls = a.status === 'active' ? (a.weight > 0 ? 'is-good' : 'is-bad') : a.status === 'expired' ? 'is-bad' : 'is-warn';
          const pct = Math.max(0, Math.min(100, a.weight));
          const expPct = a.days_left === null ? 0 : Math.max(0, Math.min(100, (a.days_left / 30) * 100));
          // Magpie 的余额卡：名字在左，数字在右，右对齐。
          // 失效的号不该显示「剩余权重 100」——它已经不能用了。
          // 那时候真正要说的是「过期几天 / 什么时候过期」。
          const dead = a.status === 'expired' || a.status === 'disabled';
          const figLabel = a.status === 'expired' ? '过期于'
            : a.status === 'disabled' ? '状态'
            : '剩余权重';
          const figValue = a.status === 'expired'
            ? (a.days_left === null ? '未知' : Math.abs(a.days_left) + ' 天')
            : a.status === 'disabled' ? '已停用'
            : String(a.weight);
          return `
          <div class="gauge-card">
            <div class="gauge-card-top">
              <span class="gauge-card-id">${esc(a.openid)}</span>
              ${ledTag(a.status === 'active' ? (a.weight > 0 ? '在岗' : '归零') : a.status === 'expired' ? '失效' : a.status,
                a.status === 'active' ? (a.weight > 0 ? 'ok' : 'bad') : a.status === 'expired' ? 'bad' : 'warn')}
            </div>
            <div class="gauge-card-fig">
              <span class="gauge-card-fig-label">${esc(figLabel)}</span>
              <span class="gauge-card-big">${esc(figValue)}${dead ? '' : '<span class="muted" style="font-size:11px"> / 100</span>'}</span>
            </div>
            ${dead ? '' : `
            <div class="gauge">
              <div class="gauge-track"><span class="gauge-fill ${cls === 'is-good' ? '' : cls}" style="width:${pct}%"></span></div>
              <div class="gauge-caption"><span>成功 ${esc(fmtInt(a.ok_count))}</span><span>失败 ${esc(a.fail_count)}</span></div>
            </div>`}
            <div class="expiry ${a.days_left !== null && a.days_left < 0 ? 'is-bad' : a.days_left !== null && a.days_left < 4 ? 'is-warn' : ''}">
              <i style="width:${expPct.toFixed(1)}%"></i>
            </div>
            <div class="gauge-caption">
              <span>${a.days_left === null ? '有效期未知' : (a.days_left < 0 ? '已过期 ' + Math.abs(a.days_left) + ' 天' : '余 ' + a.days_left + ' 天')}</span>
              <span>${a.cooldown > 0 ? '冷却 ' + a.cooldown + 's' : '在途 ' + a.inflight}</span>
            </div>
          </div>`;
        }).join('') || '<div class="empty"><div class="empty-title">池子是空的</div><div class="empty-text">去「账号池」加一个号。</div></div>'}
      </div>`);

    /* ---- 运维动作 ---- */
    const ops = h(`
      <div class="row">
        <button class="btn btn-ghost" data-op="cleanup">清理过期产物</button>
        <button class="btn btn-ghost" data-op="backup">立即备份</button>
        <button class="btn btn-ghost" data-op="refresh">提前续期到期号</button>
        <button class="btn btn-ghost" data-op="trace">原始链路 JSON</button>
      </div>`);

    ops.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-op]');
      if (!btn) return;
      const op = btn.dataset.op;

      /* 「原始链路 JSON」以前是个指向 /admin/trace 的直链。<a> 带不了
         X-Admin-Token 头，所以一旦 config.json 里设了 admin_token，点它只会得到 401。
         改成先同步开窗（await 之后再开会被弹窗拦截），拿到数据再自己写进去。 */
      if (op === 'trace') {
        const win = window.open('', '_blank');
        btn.disabled = true;
        try {
          const r = await api.get('/admin/trace?limit=200&minutes=1440');
          const text = JSON.stringify(r, null, 2);
          if (win && win.document) {
            win.document.title = '原始链路 JSON';
            win.document.body.textContent = '';
            const pre = win.document.createElement('pre');
            pre.style.cssText = 'font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;'
              + 'white-space:pre-wrap;word-break:break-all;padding:16px;margin:0';
            pre.textContent = text;
            win.document.body.appendChild(pre);
          } else {
            // 弹窗被拦下时退化成下载
            const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
            const a = document.createElement('a');
            a.href = url;
            a.download = 'trace.json';
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 10000);
          }
        } catch (err) {
          if (win) win.close();
          toastError(err);
        } finally {
          btn.disabled = false;
        }
        return;
      }

      const paths = { cleanup: '/admin/cleanup', backup: '/admin/backup', refresh: '/admin/refresh-ahead' };
      const names = { cleanup: '清理', backup: '备份', refresh: '续期' };
      btn.disabled = true;
      btn.textContent = '执行中…';
      try {
        const r = await api.post(paths[op]);
        if (op === 'cleanup') {
          toast(`清理完成：删文件 ${r.removed_files} · 删任务 ${r.removed_tasks} · 删用量 ${r.removed_usage}`, 'good');
        } else if (op === 'backup') {
          toast('备份完成', 'good');
        } else {
          toast(`检查了 ${r.checked} 个号，续期 ${(r.refreshed || []).length} 个`, (r.refreshed || []).length ? 'good' : undefined);
        }
      } catch (err) {
        toastError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = names[op] + (op === 'cleanup' ? '过期产物' : op === 'backup' ? '备份' : '到期号');
      }
    });

    root.appendChild(strip);
    if (alerts.length) root.appendChild(alertBox);

    const left = h(`<div class="stack"></div>`);
    left.appendChild(waveHead);
    left.appendChild(waveBox);
    left.appendChild(h(`<div class="shead"><span class="shead-label">账号余量</span><span class="shead-rule"></span>
      <span class="shead-meta">${accRows.length} 个号</span></div>`));
    left.appendChild(gauges);

    const right = h(`<div class="stack"></div>`);
    right.appendChild(h(`<div class="shead"><span class="shead-label">状态台账</span><span class="shead-rule"></span></div>`));
    right.appendChild(ledger);
    right.appendChild(h(`<div class="shead"><span class="shead-label">运维动作</span><span class="shead-rule"></span></div>`));
    right.appendChild(ops);

    const split = h(`<div class="split"></div>`);
    split.appendChild(left);
    split.appendChild(right);
    root.appendChild(split);

    return {
      node: root,
      onMount: () => { drawWave(); },
    };
  },
};
