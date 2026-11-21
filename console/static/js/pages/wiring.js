/* ============================================================
   pages/wiring.js —— 接线
   把本机的 agent 工具（Claude Code / Codex / DSH / zcode）指向这个代理。
   写操作会改 ~/.claude 等目录；不给 key 也行 —— 服务端会自己发一把接入钥匙（mkx_）。
   ============================================================ */

import {
  api, h, esc, store, toast, toastError, ledTag,
} from '../core.js';

const EFFORTS = [
  { v: '', label: '不指定' },
  { v: 'low', label: 'low' },
  { v: 'medium', label: 'medium' },
  { v: 'high', label: 'high' },
];

export default {
  sub: '改写本机 agent 工具的配置，让它们把请求打到这个代理上。会先备份再改，rollback 能退回去。',

  async render() {
    const root = h(`<div class="stack"></div>`);

    let agentsPayload = null;
    let modelsPayload = null;
    try {
      agentsPayload = await api.get('/v1/wire/agents');
      modelsPayload = await api.get('/v1/wire/models');
    } catch (e) {
      return { node: h(`<div class="notice is-bad">读不到接线信息：${esc(e.message)}</div>`) };
    }

    const agents = agentsPayload.agents || [];
    const models = modelsPayload.models || [];

    /* ---- 客户端 key ---- */
    const keyInput = h(`<input class="input mono" type="password" autocomplete="off"
      placeholder="mvk_…（在「密钥」页创建）">`);
    keyInput.value = store.clientKey;

    const keyNote = h(`<p class="field-hint">
      留空就行：服务端会自动发一把<strong>接入钥匙</strong>（<code>mkx_</code> 开头）写进配置，
      它不在「密钥」页的列表里，也不用管，删了下次接线会重新发。
      填了就写你填的那把（想让自己能单独停掉某个工具的对话，就填一把自己的）。</p>`);

    keyInput.addEventListener('change', () => {
      store.clientKey = keyInput.value.trim();
    });

    /* ---- 表单 ---- */
    const modelSel = h(`<select class="select">
      ${models.map((m) => `<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`).join('')}
    </select>`);

    const effortSel = h(`<select class="select">
      ${EFFORTS.map((e) => `<option value="${esc(e.v)}">${esc(e.label)}</option>`).join('')}
    </select>`);

    const profileInput = h(`<input class="input" placeholder="DSH 专用，如 desktop">`);

    const checkboxes = agents.map((a) => h(`
      <label class="row-tight" style="gap:7px;cursor:${a.installed ? 'pointer' : 'not-allowed'};opacity:${a.installed ? 1 : 0.45}">
        <input type="checkbox" value="${esc(a.id)}" ${a.installed ? 'checked' : ''} ${a.installed ? '' : 'disabled'}>
        <span class="small">${esc(a.display_name)}</span>
      </label>`));

    const form = h(`
      <div class="well" style="padding:16px">
        <div class="form-row">
          <div class="field grow">
            <label class="field-label" for="w-model">模型</label>
            <div id="w-model-slot">${''}</div>
          </div>
          <div class="field">
            <label class="field-label" for="w-effort">思考强度</label>
            <div id="w-effort-slot">${''}</div>
          </div>
          <div class="field">
            <label class="field-label" for="w-profile">DSH profile</label>
            <div id="w-profile-slot">${''}</div>
          </div>
        </div>
        <div class="field" style="margin-top:14px">
          <span class="field-label">目标工具</span>
          <div class="row" id="w-agents">${''}</div>
        </div>
        <div class="field" style="margin-top:14px">
          <label class="field-label" for="w-key">客户端 key</label>
          <div id="w-key-slot">${''}</div>
          <div id="w-key-note-slot">${''}</div>
        </div>
        <div class="row" style="margin-top:16px">
          <button class="btn btn-primary" id="w-apply">接线</button>
          <button class="btn btn-ghost" id="w-rollback">回滚</button>
          <span class="small muted" id="w-target"></span>
        </div>
        <div class="stack" style="gap:6px;margin-top:12px" id="w-result"></div>
      </div>`);

    form.querySelector('#w-model-slot').appendChild(modelSel);
    form.querySelector('#w-effort-slot').appendChild(effortSel);
    form.querySelector('#w-profile-slot').appendChild(profileInput);
    form.querySelector('#w-key-slot').appendChild(keyInput);
    form.querySelector('#w-key-note-slot').appendChild(keyNote);
    const agentsBox = form.querySelector('#w-agents');
    checkboxes.forEach((c) => agentsBox.appendChild(c));

    const targetEl = form.querySelector('#w-target');
    function updateTarget() {
      const ids = Array.from(agentsBox.querySelectorAll('input:checked')).map((i) => i.value);
      targetEl.textContent = ids.length ? '将改动：' + ids.join('、') : '先勾选要改的工具';
    }
    agentsBox.addEventListener('change', updateTarget);
    updateTarget();

    /* ---- agent 卡片 ---- */
    const cards = h(`<div class="agents">
      ${agents.map((a) => `
        <div class="agent">
          <div class="agent-top">
            <div class="agent-name">
              <span class="agent-glyph" aria-hidden="true">${esc(a.glyph || '·')}</span>
              <div>
                <div class="agent-title">${esc(a.display_name)}</div>
                <div class="agent-sub">${esc(a.id)}</div>
              </div>
            </div>
            ${ledTag(a.installed ? (a.wired ? '已接线' : '未接线') : '未安装',
              a.installed ? (a.wired ? 'ok' : 'idle') : 'warn')}
          </div>

          <div>
            <div class="micro">当前模型</div>
            <div class="agent-model">${esc(a.current && a.current.model ? a.current.model : '—')}</div>
          </div>

          <div>
            <div class="micro">接线写入</div>
            <div class="agent-model">${a.applied
              ? `${esc(a.applied.model)}${a.applied.effort ? ' · ' + esc(a.applied.effort) : ''}`
              : '<span class="muted">还没写过</span>'}</div>
            ${a.applied ? `<div class="agent-path" style="margin-top:3px">${esc(a.applied.at || '')}</div>` : ''}
          </div>

          <div>
            <div class="micro">配置文件</div>
            ${(a.files || []).map((f) => `<div class="agent-path">${esc(f)}</div>`).join('') || '<div class="agent-path muted">—</div>'}
          </div>

          ${a.current && a.current.base_url ? `<div class="agent-path">base_url ${esc(a.current.base_url)}</div>` : ''}
          ${a.needs_restart ? '<div class="agent-warn">改完要重启这个工具才会生效</div>' : ''}
          ${(a.warnings || []).map((w) => `<div class="agent-warn">${esc(w)}</div>`).join('')}
        </div>`).join('')}
    </div>`);

    /* ---- 网关地址 ---- */
    const gw = agentsPayload.gateway || '';
    const gatewayBlock = h(`<div class="ledger">
      <div class="ledger-row"><dt>网关地址</dt><dd class="mono">${esc(gw)}</dd></div>
      <div class="ledger-row"><dt>Anthropic 入口</dt><dd class="mono">${esc(modelsPayload.anthropic_url || '—')}</dd></div>
      <div class="ledger-row"><dt>OpenAI 入口</dt><dd class="mono">${esc(modelsPayload.openai_url || '—')}</dd></div>
      <div class="ledger-row"><dt>主目录</dt><dd class="mono">${esc(agentsPayload.home || '—')}</dd></div>
    </div>`);

    /* ---- 写操作 ---- */
    const applyBtn = form.querySelector('#w-apply');
    const rollBtn = form.querySelector('#w-rollback');
    const resultBox = form.querySelector('#w-result');

    /* 服务端返回的 warnings 以前在这一层被丢掉了 —— 里面写着「写进配置的是本服务
       自动发的 mkx_ 钥匙」这类话。用户看不到，就不知道自己的配置里到底是什么。 */
    function showResult(lines) {
      resultBox.textContent = '';
      (lines || []).forEach((ln) => {
        resultBox.appendChild(h(`<div class="notice is-${ln.kind || 'warn'}">${esc(ln.text)}</div>`));
      });
    }

    /* 表单里填了就用填的，否则用 localStorage 里存的那把；都没有就是空串 ——
       空串的含义是「我不给，让服务端发」，不是「出错了」。 */
    function currentKey() {
      return keyInput.value.trim() || store.clientKey || '';
    }

    applyBtn.addEventListener('click', async () => {
      const ids = Array.from(agentsBox.querySelectorAll('input:checked')).map((i) => i.value);
      if (!ids.length) { toast('先勾选要改的工具', 'bad'); return; }
      const key = currentKey();

      const body = {
        model: modelSel.value,
        effort: effortSel.value || undefined,
        agents: ids,
        profile: profileInput.value.trim() || undefined,
      };
      // 没有 key 就不带：服务端会自己发一把接入钥匙（mkx_）写进配置。
      // 以前这里直接 toast 拦回，把服务端已经允许的操作挡在了浏览器里 ——
      // 全新机器上（库里一把 key 都没有）控制台会完全接不上线。
      if (key) body.key = key;

      applyBtn.disabled = true;
      applyBtn.textContent = '接线中…';
      showResult([]);
      try {
        const r = await api.post('/v1/wire/apply', body, key ? { 'X-API-Key': key } : undefined);
        const results = r.results || [];
        const okCount = results.filter((x) => x.ok).length;
        toast(`接线完成：${okCount}/${results.length} 个工具已改写`,
          okCount === results.length ? 'good' : 'bad');

        const lines = [];
        results.filter((x) => !x.ok).forEach((x) => lines.push({
          kind: 'bad', text: `${x.agent || x.id}：${x.error || '失败'}`,
        }));
        (r.not_installed || []).forEach((id) => lines.push({
          kind: 'warn', text: `${id} 没装，已跳过`,
        }));
        (r.warnings || []).forEach((w) => lines.push({ kind: 'warn', text: w }));
        showResult(lines);
      } catch (e) {
        toastError(e);
        showResult([{ kind: 'bad', text: e.message || String(e) }]);
      } finally {
        applyBtn.disabled = false;
        applyBtn.textContent = '接线';
      }
    });

    rollBtn.addEventListener('click', async () => {
      const ids = Array.from(agentsBox.querySelectorAll('input:checked')).map((i) => i.value);
      if (!ids.length) { toast('先勾选要回滚的工具', 'bad'); return; }
      if (!window.confirm('回滚会把这些工具的配置恢复成快照。继续？')) return;
      const key = currentKey();

      // 回滚是把文件还原、不写任何 key，所以服务端根本不需要 key
      // （`api/wire.py` 的 rollback 用 `_build(..., need_key=False)`）。
      const body = { agents: ids };
      if (key) body.key = key;

      rollBtn.disabled = true;
      rollBtn.textContent = '回滚中…';
      showResult([]);
      try {
        const r = await api.post('/v1/wire/rollback', body, key ? { 'X-API-Key': key } : undefined);
        toast('已回滚到快照', 'good');
        const lines = (r.results || []).filter((x) => !x.ok).map((x) => ({
          kind: 'bad', text: `${x.agent || x.id}：${x.error || '回滚失败'}`,
        }));
        showResult(lines);
      } catch (e) {
        toastError(e);
        showResult([{ kind: 'bad', text: e.message || String(e) }]);
      } finally {
        rollBtn.disabled = false;
        rollBtn.textContent = '回滚';
      }
    });

    root.appendChild(h(`<div class="shead">
      <span class="shead-label">接线</span><span class="shead-rule"></span>
      <span class="shead-meta">${agents.filter((a) => a.wired).length} / ${agents.filter((a) => a.installed).length} 已接</span>
    </div>`));
    root.appendChild(form);
    root.appendChild(h(`<div class="shead">
      <span class="shead-label">工具状态</span><span class="shead-rule"></span>
    </div>`));
    root.appendChild(cards);
    root.appendChild(h(`<div class="shead">
      <span class="shead-label">接入点</span><span class="shead-rule"></span>
    </div>`));
    root.appendChild(gatewayBlock);

    return { node: root };
  },
};
