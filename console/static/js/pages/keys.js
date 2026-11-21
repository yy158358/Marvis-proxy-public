/* ============================================================
   pages/keys.js —— 客户端密钥
   给 /v1/* 用的 key。明文只在创建那一刻出现一次。
   ============================================================ */

import {
  api, h, esc, fmtInt, fmtTime, fmtAgo, ledTag, toast, toastError,
  openModal, closeModal, countUp,
} from '../core.js';

export default {
  sub: '客户端访问 /v1/* 要带的 key。库里存的是哈希，明文只在新建时显示一次。',

  async render() {
    const root = h(`<div class="stack"></div>`);

    const noteInput = h(`<input class="input" placeholder="给谁用的，比如「笔记本 · Cursor」">`);

    const createBtn = h(`<button class="btn btn-primary" type="button">新建 key</button>`);

    const strip = h(`<div class="strip">
      <div class="readout"><span class="readout-label">已启用</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">把</span></span>
        <span class="readout-note" data-note="en">—</span></div>
      <div class="readout"><span class="readout-label">总调用</span>
        <span class="readout-value"><span class="rv-main">0</span><span class="readout-unit">次</span></span>
        <span class="readout-note" data-note="use">—</span></div>
      <div class="readout"><span class="readout-label">最近使用</span>
        <span class="readout-value" style="font-size:20px">—</span>
        <span class="readout-note" data-note="last">—</span></div>
    </div>`);

    const tableWrap = h(`<div class="tbl-wrap"></div>`);

    async function reload() {
      const data = await api.get('/admin/keys');
      const keys = data.keys || [];
      const enabled = keys.filter((k) => k.enabled).length;
      const uses = keys.reduce((s, k) => s + (k.uses || 0), 0);
      const last = keys.reduce((m, k) => Math.max(m, k.last_used || 0), 0);

      const cells = strip.querySelectorAll('.rv-main');
      countUp(cells[0], enabled, fmtInt);
      countUp(cells[1], uses, fmtInt);
      strip.children[2].querySelector('.readout-value').textContent = last ? fmtAgo(last) : '从未';

      const notes = strip.querySelectorAll('[data-note]');
      const disabled = keys.length - enabled;
      const topKey = keys.slice().sort((a, b) => (b.uses || 0) - (a.uses || 0))[0];
      notes[0].textContent = keys.length
        ? `${keys.length} 把${disabled ? ` · ${disabled} 把已停用` : ''}`
        : '一把都没有';
      notes[1].textContent = topKey && topKey.uses
        ? `最多的一把用了 ${fmtInt(topKey.uses)} 次`
        : '还没有调用记录';
      notes[2].textContent = last ? '按最后一次调用算' : '从未被使用过';

      if (!keys.length) {
        tableWrap.innerHTML = `<div class="empty">
          <div class="empty-title">一把 key 都没有</div>
          <div class="empty-text">客户端访问 <code>/v1/*</code> 需要 <code>X-API-Key</code> 或
            <code>Authorization: Bearer …</code>。建一把，填上备注，别丢。</div>
        </div>`;
        return;
      }

      tableWrap.innerHTML = `
        <table class="tbl">
          <thead><tr>
            <th>前缀</th><th>备注</th><th>状态</th>
            <th class="r">调用次数</th><th>创建于</th><th>最近使用</th><th class="r">操作</th>
          </tr></thead>
          <tbody>
            ${keys.map((k) => `
              <tr data-id="${esc(k.id)}">
                <td class="nowrap mono cell-strong">${esc(k.prefix || k.id)}</td>
                <td><div class="cell-note">${esc(k.note || '—')}</div></td>
                <td>${ledTag(k.enabled ? '启用' : '停用', k.enabled ? 'ok' : 'idle')}</td>
                <td class="r">${esc(fmtInt(k.uses || 0))}</td>
                <td class="nowrap cell-mute">${esc(fmtTime(k.created))}</td>
                <td class="nowrap cell-mute">${esc(k.last_used ? fmtAgo(k.last_used) : '从未')}</td>
                <td class="r">
                  <div class="row-actions">
                    <button class="btn btn-xs btn-ghost" data-act="toggle">${k.enabled ? '停用' : '启用'}</button>
                    <button class="btn btn-xs btn-danger" data-act="delete">删除</button>
                  </div>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>`;
    }

    createBtn.addEventListener('click', async () => {
      const note = noteInput.value.trim();
      createBtn.disabled = true;
      createBtn.textContent = '创建中…';
      try {
        const r = await api.post('/admin/keys', { note });
        const body = h(`<div class="stack" style="gap:12px">
          <div class="notice is-warn">
            <strong>明文只显示这一次。</strong>关掉就再也拿不回来了，库里只存哈希。
          </div>
          <div class="copybox" id="k-plain"><code>${esc(r.key)}</code><button type="button">复制</button></div>
          <p class="field-hint">${esc(r.hint || '')}</p>
          <div>
            <p class="field-hint" style="margin-bottom:6px">客户端用法，二选一：</p>
            <div class="codeblock">
              <code>X-API-Key: ${esc(r.key)}</code>
              <code>Authorization: Bearer ${esc(r.key)}</code>
            </div>
          </div>
        </div>`);
        body.querySelector('#k-plain button').addEventListener('click', () => {
          import('../core.js').then((m) => m.copyText(r.key, 'key'));
        });
        openModal({
          title: 'key 已创建',
          body,
          actions: [{ label: '我存好了', kind: 'btn-primary', onClick: () => closeModal() }],
        });
        noteInput.value = '';
        await reload();
      } catch (e) {
        toastError(e);
      } finally {
        createBtn.disabled = false;
        createBtn.textContent = '新建 key';
      }
    });

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
          if (!window.confirm('删除这把 key？用它访问的客户端会立刻被拒。')) return;
          await api.post(`/admin/keys/${id}/delete`);
          toast('已删除', 'good');
        } else {
          const isEnabled = label === '停用';
          await api.post(`/admin/keys/${id}/${isEnabled ? 'disable' : 'enable'}`);
          toast(`已${isEnabled ? '停用' : '启用'}`, 'good');
        }
        await reload();
      } catch (err) {
        toastError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });

    root.appendChild(strip);
    root.appendChild(h(`<div class="shead">
      <span class="shead-label">新建</span><span class="shead-rule"></span>
    </div>`));
    root.appendChild(h(`<div class="well" style="padding:16px">
      <div class="form-row">
        <div class="field grow">
          <label class="field-label" for="k-note">备注</label>
          <div id="k-note-slot"></div>
        </div>
        <div id="k-btn-slot"></div>
      </div>
      <p class="field-hint" style="margin-top:10px">
        库里只保存哈希，<code>prefix</code> 是前 12 位，用于界面上区分。
        停用比删除安全：客户端立刻被拒，但你还能改回来。
      </p>
    </div>`));
    root.querySelector('#k-note-slot').appendChild(noteInput);
    root.querySelector('#k-btn-slot').appendChild(createBtn);
    root.appendChild(h(`<div class="shead">
      <span class="shead-label">已有 key</span><span class="shead-rule"></span>
    </div>`));
    root.appendChild(tableWrap);

    await reload();
    return { node: root };
  },
};
