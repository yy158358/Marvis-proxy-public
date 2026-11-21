/* ============================================================
   app.js —— 控制台外壳：导航、路由、页头、全局刷新
   ============================================================ */

import { api, esc, h, toast, toastError, store, initModal } from './core.js';

import overview from './pages/overview.js';
import accounts from './pages/accounts.js';
import usage from './pages/usage.js';
import models from './pages/models.js';
import wiring from './pages/wiring.js';
import keys from './pages/keys.js';
import routing from './pages/routing.js';

const ROUTES = [
  { id: 'overview', label: '概览', en: 'Overview', page: overview },
  { id: 'accounts', label: '账号池', en: 'Accounts', page: accounts },
  { id: 'usage', label: '用量', en: 'Traffic', page: usage },
  { id: 'models', label: '模型', en: 'Models', page: models },
  { id: 'wiring', label: '接线', en: 'Wiring', page: wiring },
  { id: 'keys', label: '密钥', en: 'Keys', page: keys },
  { id: 'routing', label: '路由', en: 'Routing', page: routing },
];

const view = document.getElementById('view');
const nav = document.getElementById('nav');
const titleEl = document.getElementById('page-title');
const subEl = document.getElementById('page-sub');
const stampEl = document.getElementById('stamp');

let current = null;      // 当前路由对象
let cleanup = null;      // 当前页面的清理函数

/* ---------------- 导航 ---------------- */

function buildNav() {
  nav.innerHTML = '';
  ROUTES.forEach((r) => {
    const a = h(
      `<a class="nav-item" href="#/${r.id}" data-route="${r.id}">
        <span class="nav-chan" aria-hidden="true"></span>
        <span class="nav-label">${esc(r.label)}</span>
        <span class="nav-en">${esc(r.en)}</span>
      </a>`
    );
    nav.appendChild(a);
  });
}

function markNav(id) {
  nav.querySelectorAll('.nav-item').forEach((el) => {
    el.classList.toggle('is-active', el.dataset.route === id);
    if (el.dataset.route === id) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
}

/* ---------------- 路由 ---------------- */

function routeId() {
  const raw = (location.hash || '').replace(/^#\/?/, '').split(/[?#]/)[0];
  // 旧的 #/trace 链接别作废：这一页从「链路」改名成了「路由」
  if (raw === 'trace') return 'routing';
  return ROUTES.some((r) => r.id === raw) ? raw : 'overview';
}

async function renderRoute() {
  const id = routeId();
  const r = ROUTES.find((x) => x.id === id);

  if (cleanup) { try { cleanup(); } catch (_) {} cleanup = null; }
  current = r;
  markNav(id);

  titleEl.textContent = r.label;
  subEl.textContent = r.page.sub || '';
  document.title = `${r.label} · Marvis Proxy`;

  view.innerHTML = '';
  view.classList.add('is-loading');
  stampEl.textContent = '读取中';

  try {
    const result = await r.page.render();
    view.innerHTML = '';
    if (result && result.node) view.appendChild(result.node);
    if (result && result.cleanup) cleanup = result.cleanup;
    // 图表要在进 DOM 之后才能量到真实宽度，标签才不会被拉变形
    if (result && result.onMount) result.onMount();
  } catch (e) {
    view.innerHTML = '';
    view.appendChild(h(
      `<div class="notice is-bad">
        <strong>这一页没能读出来。</strong><br>
        ${esc(e && e.message ? e.message : String(e))}
      </div>`
    ));
    toastError(e);
  } finally {
    view.classList.remove('is-loading');
    stampEl.textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }
}

/* ---------------- 全局刷新 ---------------- */

async function refresh() {
  await renderRoute();
  await refreshFooter();
}

async function refreshFooter() {
  try {
    const health = await api.get('/health');
    const bind = health.bind || {};
    const bindEl = document.getElementById('foot-bind');
    bindEl.textContent = `${bind.host || '?'}:${bind.port || '?'}`;
    bindEl.classList.toggle('warn', bind.lan_reachable === false);

    document.getElementById('foot-ver').textContent = health.version || '—';
    document.getElementById('foot-pid').textContent = health.pid ? 'PID ' + health.pid : '—';
  } catch (e) {
    /* 侧栏读不到不影响主区 */
  }
}

/* ---------------- 管理令牌 ---------------- */

function initToken() {
  const box = document.getElementById('token-box');
  const input = document.getElementById('token-input');
  const btn = document.getElementById('token-save');
  const toggle = document.getElementById('token-toggle');

  input.value = store.adminToken;

  toggle.addEventListener('click', () => {
    const open = box.hidden;
    box.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) input.focus();
  });

  btn.addEventListener('click', () => {
    store.adminToken = input.value.trim();
    box.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toast(store.adminToken ? '令牌已保存' : '令牌已清除', 'good');
    refresh();
  });

  window.addEventListener('marvis:unauthorized', () => {
    box.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    toast('服务端要管理令牌：填进左下角「管理令牌」', 'bad');
  });
}

/* ---------------- 启动 ---------------- */

function boot() {
  buildNav();
  initModal();
  initToken();

  document.getElementById('btn-reload').addEventListener('click', () => {
    refresh();
    toast('已重新读取');
  });

  window.addEventListener('hashchange', renderRoute);

  // 令牌框点外面不自动关，Esc 关
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const box = document.getElementById('token-box');
      if (!box.hidden) {
        box.hidden = true;
        document.getElementById('token-toggle').setAttribute('aria-expanded', 'false');
      }
    }
  });

  renderRoute();
  refreshFooter();
  setInterval(refreshFooter, 15000);
}

boot();
