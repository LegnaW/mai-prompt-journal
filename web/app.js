// ============================================================
// 麦麦的绘图笔记本 WebUI — 共享逻辑
// ============================================================

function api(method, path, body) {
  const opts = { method, headers: {}, cache: 'no-store' };
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  return fetch(path, opts).then(r => {
    if (r.status === 401) { showLogin(); throw new Error('unauthorized'); }
    return r.json();
  });
}

function esc(s) { const d = document.createElement('div'); d.textContent = s||''; return d.innerHTML; }

function showLogin() { document.getElementById('loginOverlay').classList.remove('hidden'); }

function doLogin() {
  const pwd = document.getElementById('loginPassword').value;
  api('POST', '/api/login', { password: pwd }).then(data => {
    if (data.error) { alert(data.error); return; }
    document.getElementById('loginOverlay').classList.add('hidden');
    loadStatus();
  }).catch(() => {});
}

function toggleCard(name) {
  const body = document.getElementById(name + 'CardBody');
  const btn = document.getElementById(name + 'ToggleBtn');
  const hidden = body.classList.toggle('hidden');
  btn.textContent = hidden ? '展开' : '收起';
}

// ============================================================
// 状态栏 + 笔记本下拉（当前页面存在的元素才填充）
// ============================================================

function loadStatus() {
  api('GET', '/api/status').then(data => {
    const bar = document.getElementById('statusBar');
    if (bar) {
      bar.innerHTML = '';
      (data.notebooks || []).forEach(nb => {
        const cls = nb.status === 'ok' ? 'ok' : nb.status === 'stale' ? 'stale' : 'no-index';
        const icon = nb.status === 'ok' ? '✓' : nb.status === 'stale' ? '✗' : '○';
        bar.innerHTML += `<span class="nb-badge ${cls}">${nb.name} (${nb.count}${icon})</span>`;
      });
      if (!data.notebooks || data.notebooks.length === 0) bar.innerHTML = '<span style="color:#999">暂无笔记本</span>';
    }
    document.querySelectorAll('.notebook-select').forEach(sel => {
      const allowAll = sel.dataset.all === '1';
      let opts = allowAll
        ? '<option value="default">default</option><option value="all">全部</option>'
        : '<option value="default">default</option>';
      (data.notebooks || []).forEach(nb => {
        if (nb.name !== 'default') opts += `<option value="${nb.name}">${nb.name}</option>`;
      });
      sel.innerHTML = opts;
    });
  }).catch(() => {});
}

// ============================================================
// 顶部导航（每页注入，参数为当前页标识）
// ============================================================

const NAV_ITEMS = [
  { id: 'index', href: '/web/index.html', label: '📒 首页' },
  { id: 'dedup', href: '/web/dedup.html', label: '🔄 去重' },
  { id: 'organize', href: '/web/organize.html', label: '🤖 操作数据库' },
];

function injectNav(activeId) {
  const holder = document.getElementById('nav');
  if (!holder) return;
  holder.innerHTML = '<nav class="topnav">' + NAV_ITEMS.map(item =>
    `<a href="${item.href}" class="${item.id === activeId ? 'active' : ''}">${item.label}</a>`
  ).join('') + '</nav>';
}

// ============================================================
// 登录遮罩（所有页面统一注入）
// ============================================================

function ensureLoginOverlay() {
  if (document.getElementById('loginOverlay')) return;
  const div = document.createElement('div');
  div.className = 'login-overlay hidden';
  div.id = 'loginOverlay';
  div.innerHTML = `
    <div class="login-box">
      <h3>🔑 请输入访问密码</h3>
      <input type="password" id="loginPassword" placeholder="密码" onkeydown="if(event.key==='Enter')doLogin()">
      <br>
      <button class="btn" onclick="doLogin()">确认</button>
    </div>`;
  document.body.appendChild(div);
}

// ============================================================
// 当前活跃任务（所有页面统一注入 + 轮询）
// ============================================================

let taskPollTimer = null;

function injectTaskCenter() {
  if (document.getElementById('taskCenter')) return;
  const div = document.createElement('div');
  div.className = 'card';
  div.id = 'taskCenter';
  div.innerHTML = `
    <div class="row" style="justify-content: space-between; margin-bottom: 8px;">
      <strong>⚙️ 当前活跃任务</strong>
    </div>
    <div id="taskList" style="font-size: 13px;">加载中...</div>`;
  const container = document.querySelector('.container');
  if (container) {
    container.insertBefore(div, container.firstChild);
  } else {
    document.body.insertBefore(div, document.body.firstChild);
  }
}

function renderTaskProgress(t) {
  const p = t.progress || {};
  if (p.total) {
    const pct = Math.round((p.done || 0) / p.total * 100);
    return `处理中: ${esc(p.current || '')} (${p.done || 0}/${p.total}) ${pct}%`;
  }
  return '处理中...';
}

function renderTaskSummary(result) {
  const lines = (result && result.results || []).map(r =>
    r.error ? `${esc(r.notebook)}: 失败` : `${esc(r.notebook)}: ${r.total}条(复用${r.reused},新建${r.rebuilt})`
  ).join('；');
  return lines || '完成';
}

function renderTaskItem(t) {
  let body;
  if (t.status === 'running') {
    body = `<span style="color:#4f9afe">⏳ ${renderTaskProgress(t)}</span>`;
  } else if (t.status === 'done') {
    body = `<span style="color:#2e7d32">✅ 完成 — ${renderTaskSummary(t.result)}</span>`;
  } else {
    body = `<span style="color:#c62828">❌ 失败 — ${esc(t.error || '未知错误')}</span>`;
  }
  return `<div style="padding:6px 0;border-bottom:1px solid #f0f0f0;">
    <strong>${esc(t.label || t.type)}</strong> ${body}
  </div>`;
}

function pollTasks() {
  api('GET', '/api/tasks').then(data => {
    const list = document.getElementById('taskList');
    if (!list) return;
    const tasks = data.tasks || [];
    if (tasks.length === 0) {
      list.innerHTML = '<span style="color:#999">暂无任务</span>';
      stopTaskPoll();
      return;
    }
    const hasRunning = tasks.some(t => t.status === 'running');
    const prevRunning = document.getElementById('taskCenter').dataset.running === '1';
    list.innerHTML = tasks.map(renderTaskItem).join('');
    if (hasRunning) {
      startTaskPoll();
    } else {
      stopTaskPoll();
    }
    document.getElementById('taskCenter').dataset.running = hasRunning ? '1' : '0';
    // 任务从运行态变为结束态时刷新状态栏（笔记本数量/索引状态可能变化）
    if (prevRunning && !hasRunning) loadStatus();
  }).catch(() => {});
}

function startTaskPoll() {
  if (taskPollTimer) return;
  taskPollTimer = setInterval(pollTasks, 2000);
}

function stopTaskPoll() {
  if (taskPollTimer) {
    clearInterval(taskPollTimer);
    taskPollTimer = null;
  }
}

ensureLoginOverlay();
injectTaskCenter();
pollTasks();
