/* ============================================================
   pages/accounts.js —— 账号池
   加号 → 看池子 → 续期 / 禁用 / 删除
   ============================================================ */

import {
  api, h, esc, fmtInt, fmtAgo, fmtTime,
  ledTag, toast, toastError, copyBox, countUp, fmtCompact,
} from '../core.js';

async function safe(p) {
  try { return await p; } catch (e) { console.warn(e); return null; }
}

const STATE_KIND = { active: 'ok', disabled: 'warn', expired: 'bad' };
// 接口给的是原始英文状态词，界面上要说人话
const STATE_LABEL = { active: '在岗', disabled: '已停用', expired: '已失效' };

export default {
  sub: '池子里每个号的状态、权重和有效期。401 会把权重打到 0 并冷却，冷却结束会自动半开试探。',

  async render() {
    const root = h(`<div class="stack"></div>`);

    /* ---------------- 读数带 ---------------- */
    const strip = h(`<div class="strip">
      <div class="readout is-good"><span class="readout-label">在岗</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">个</span></span>
        <span class="readout-note" data-note="act">—</span></div>
      <div class="readout"><span class="readout-label">冷却中</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">个</span></span>
        <span class="readout-note" data-note="cool">—</span></div>
      <div class="readout"><span class="readout-label">已失效</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">个</span></span>
        <span class="readout-note" data-note="exp">—</span></div>
      <div class="readout"><span class="readout-label">累计成功</span>
        <span class="readout-value"><span class="rv-main">0</span></span>
        <span class="readout-note" data-note="ok">—</span></div>
    </div>`);

    /* ---------------- 加号面板 ---------------- */
    const addPanel = h(`
      <div class="block" id="add-panel" style="display:none">
        <div class="shead"><span class="shead-label">加号</span><span class="shead-rule"></span>
          <span class="shead-meta">授权链接约 5 分钟有效</span></div>
        <div class="well" style="padding:16px">
          <div class="form-row">
            <div class="field" style="min-width:150px">
              <label class="field-label" for="login-type">登录方式</label>
              <select class="select" id="login-type">
                <option value="QC">QQ 登录</option>
                <option value="WX">微信登录</option>
              </select>
            </div>
            <button class="btn btn-primary" id="login-start">生成授权链接</button>
          </div>
          <div id="login-step2" style="display:none;margin-top:16px">
            <div class="field">
              <span class="field-label">在手机浏览器打开</span>
              <div id="login-url"></div>
              <p class="field-hint" id="login-hint"></p>
              <p class="field-hint" id="login-expire"></p>
            </div>
            <div class="field" style="margin-top:16px">
              <label class="field-label" for="login-text">把落地页整段内容粘回来</label>
              <textarea class="textarea" id="login-text" placeholder="JSON 正文优先；没有就贴地址栏整条 URL"></textarea>
              <p class="field-hint">回填后调用 <code>POST /admin/login/exchange</code>，凭证直接入库，不会回显明文。</p>
            </div>
            <div class="row" style="margin-top:12px">
              <button class="btn btn-primary" id="login-exchange">兑换并加入池子</button>
              <button class="btn btn-ghost" id="login-cancel">取消</button>
            </div>
          </div>
        </div>
      </div>`);

    const addBtn = h(`<button class="btn btn-primary" type="button">加号</button>`);
    addBtn.addEventListener('click', () => {
      const open = addPanel.style.display === 'none';
      addPanel.style.display = open ? '' : 'none';
      if (open) document.getElementById('login-type').focus();
    });

    /* ---------------- 账号表 ---------------- */
    const tableWrap = h(`<div class="tbl-wrap"></div>`);

    async function reload() {
      const data = await api.get('/admin/accounts');
      const rows = data.accounts || [];
      const c = data.counts || {};

      // 读数
      const cells = strip.querySelectorAll('.rv-main');
      countUp(cells[0], c.active || 0, (v) => String(Math.round(v)));
      countUp(cells[1], c.cooling || 0, (v) => String(Math.round(v)));
      countUp(cells[2], c.expired || 0, (v) => String(Math.round(v)));
      const totalOk = rows.reduce((s, a) => s + (a.ok_count || 0), 0);
      countUp(cells[3], totalOk, fmtCompact);
      strip.children[0].classList.toggle('is-alert', (c.expired || 0) > 0);
      strip.children[2].classList.toggle('is-alert', (c.expired || 0) > 0);

      // 副行：每个数字都要能拆开看
      const notes = strip.querySelectorAll('[data-note]');
      const totalFail = rows.reduce((s, a) => s + (a.fail_count || 0), 0);
      const expiring = rows.filter((a) => a.days_left !== null && a.days_left >= 0 && a.days_left < 4);
      notes[0].textContent = `共 ${c.total || 0} 个号在池子里`;
      notes[1].textContent = (c.cooling || 0) > 0 ? '退避结束后自动半开' : '当前没有号在退避';
      notes[2].textContent = (c.expired || 0) > 0 ? '需重新授权才能恢复' : '没有失效的号';
      notes[3].textContent = `失败 ${fmtInt(totalFail)} 次${expiring.length ? ` · ${expiring.length} 个即将到期` : ''}`;

      if (!rows.length) {
        tableWrap.innerHTML = `<div class="empty">
          <div class="empty-title">池子里还没有号</div>
          <div class="empty-text">点右上角「加号」，生成授权链接，用手机打开登录后把落地页内容粘回来。</div>
        </div>`;
        return;
      }

      tableWrap.innerHTML = `
        <table class="tbl">
          <thead><tr>
            <th>号</th><th>OpenID</th><th>状态</th><th class="r">权重</th>
            <th class="r">成功 / 失败</th><th class="r">在途</th>
            <th>最后使用</th><th>有效期</th><th>备注</th><th class="r">操作</th>
          </tr></thead>
          <tbody>
            ${rows.map((a) => {
              const kind = STATE_KIND[a.status] || 'idle';
              const days = a.days_left;
              const expText = days === null ? '未知' : days < 0 ? '已过期 ' + Math.abs(days) + '天' : '余 ' + days + '天';
              const expCls = days !== null && days < 0 ? 'is-bad' : days !== null && days < 4 ? 'is-warn' : '';
              const stateText = a.status === 'active'
                ? (a.weight > 0 ? '在岗' : '权重归零')
                : (STATE_LABEL[a.status] || a.status);
              return `<tr data-id="${a.id}">
                <td class="nowrap mono cell-strong">#${a.id}</td>
                <td class="nowrap mono">${esc(a.openid)}</td>
                <td>${ledTag(stateText, kind === 'ok' && a.weight <= 0 ? 'warn' : kind)}</td>
                <td class="r">${esc(a.weight)}</td>
                <td class="r">${esc(fmtInt(a.ok_count))} / ${esc(fmtInt(a.fail_count))}</td>
                <td class="r">${esc(a.inflight)}</td>
                <td class="nowrap cell-mute" title="${esc(fmtTime(a.last_used))}">${esc(fmtAgo(a.last_used))}</td>
                <td class="nowrap"><span class="${expCls ? 'cell-strong' : 'cell-mute'}"
                  style="${expCls === 'is-bad' ? 'color:var(--vermilion)' : expCls === 'is-warn' ? 'color:var(--amber)' : ''}">${esc(expText)}</span>
                  ${a.cooldown > 0 ? `<span class="muted small"> · 冷却 ${a.cooldown}s</span>` : ''}</td>
                <td><div class="cell-note" title="${esc(a.note || '')}">${esc(a.note || '—')}</div></td>
                <td class="r">
                  <div class="row-actions">
                    <button class="btn btn-xs btn-ghost" data-act="probe">探测</button>
                    <button class="btn btn-xs btn-ghost" data-act="refresh">续期</button>
                    <button class="btn btn-xs btn-ghost" data-act="toggle">${a.status === 'active' ? '禁用' : '启用'}</button>
                    <button class="btn btn-xs btn-danger" data-act="delete">删除</button>
                  </div>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>`;
    }

    tableWrap.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const tr = btn.closest('tr');
      const id = tr.dataset.id;
      const act = btn.dataset.act;
      const label = btn.textContent;
      btn.disabled = true;

      try {
        if (act === 'delete') {
          if (!window.confirm(`确定删除 #${id}？这个号会连同凭证一起移除，撤不回来。`)) return;
          await api.post(`/admin/accounts/${id}/delete`);
          toast(`已删除 #${id}`, 'good');
        } else if (act === 'toggle') {
          const isActive = btn.textContent === '禁用';
          await api.post(`/admin/accounts/${id}/${isActive ? 'disable' : 'enable'}`);
          toast(`#${id} 已${isActive ? '禁用' : '启用'}`, 'good');
        } else if (act === 'probe') {
          const r = await api.post(`/admin/accounts/${id}/probe`, {});
          const p = r.probe || {};
          toast(`#${id} 探测：${p.ok ? '通过' : '失败'}${p.error ? ' — ' + p.error : ''}`, p.ok ? 'good' : 'bad');
        } else if (act === 'refresh') {
          await api.post(`/admin/accounts/${id}/refresh`, {});
          toast(`#${id} 已续期`, 'good');
        }
        await reload();
      } catch (err) {
        toastError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });

    /* ---------------- 加号交互 ---------------- */
    const typeSel = addPanel.querySelector('#login-type');
    const startBtn = addPanel.querySelector('#login-start');
    const step2 = addPanel.querySelector('#login-step2');
    const urlBox = addPanel.querySelector('#login-url');
    const hintEl = addPanel.querySelector('#login-hint');
    const expireEl = addPanel.querySelector('#login-expire');

    startBtn.addEventListener('click', async () => {
      startBtn.disabled = true;
      startBtn.textContent = '生成中…';
      try {
        const r = await api.post('/admin/login/start', { type: typeSel.value });
        urlBox.innerHTML = '';
        urlBox.appendChild(copyBox(r.url, '授权链接'));
        hintEl.textContent = r.hint || '';
        expireEl.textContent = r.expires_note || '';
        step2.style.display = '';
        addPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } catch (err) {
        toastError(err);
      } finally {
        startBtn.disabled = false;
        startBtn.textContent = '重新生成链接';
      }
    });

    addPanel.querySelector('#login-cancel').addEventListener('click', () => {
      step2.style.display = 'none';
      urlBox.innerHTML = '';
      addPanel.querySelector('#login-text').value = '';
    });

    addPanel.querySelector('#login-exchange').addEventListener('click', async (e) => {
      const btn = e.target;
      const text = addPanel.querySelector('#login-text').value.trim();
      if (!text) { toast('先粘贴落地页内容', 'bad'); return; }
      btn.disabled = true;
      btn.textContent = '兑换中…';
      try {
        const r = await api.post('/admin/login/exchange', { text });
        const probe = r.probe || {};
        toast(`已加入池子 #${r.id}（${r.source}），探测${probe.ok ? '通过' : '未通过'}`, probe.ok ? 'good' : 'bad');
        step2.style.display = 'none';
        urlBox.innerHTML = '';
        addPanel.querySelector('#login-text').value = '';
        await reload();
      } catch (err) {
        toastError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = '兑换并加入池子';
      }
    });

    /* ---------------- 组装 ---------------- */
    const head = h(`<div class="shead">
      <span class="shead-label">账号池</span><span class="shead-rule"></span>
      <span class="shead-meta">点行尾按钮可探测 / 续期 / 禁用 / 删除</span>
    </div>`);
    head.appendChild(addBtn);

    root.appendChild(strip);
    root.appendChild(addPanel);
    root.appendChild(head);
    root.appendChild(tableWrap);

    await reload();

    return { node: root };
  },
};
