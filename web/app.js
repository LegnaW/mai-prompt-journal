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

// ============================================================
// 使用说明（md 渲染 + 展开栏，供各页面复用）
// ============================================================

function inlineMd(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code style="background:#f5f5f5;padding:1px 4px;border-radius:3px;">$1</code>');
}

function renderMdSimple(md) {
  const lines = md.split('\n');
  let html = '';
  let inList = false;
  let inTable = false;
  let inCode = false;
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  const closeTable = () => { if (inTable) { html += '</table>'; inTable = false; } };
  for (const raw of lines) {
    const line = raw.replace(/\r$/, '');
    const t = line.trim();
    if (/^```/.test(t)) {
      if (inCode) {
        html += '</code></pre>';
        inCode = false;
      } else {
        closeList(); closeTable();
        html += '<pre style="background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:10px;overflow-x:auto;margin:6px 0;"><code>';
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      html += esc(line) + '\n';
      continue;
    }
    if (!t) { closeList(); closeTable(); continue; }
    if (t.startsWith('|') && t.endsWith('|')) {
      if (!inTable) {
        closeList();
        html += '<table style="border-collapse:collapse;width:100%;margin:6px 0;">';
        inTable = true;
      }
      const cells = t.slice(1, -1).split('|').map(c => c.trim());
      if (cells.every(c => /^-+$/.test(c))) { continue; }
      html += '<tr>' + cells.map(c => `<td style="border:1px solid #ddd;padding:4px 8px;">${esc(c)}</td>`).join('') + '</tr>';
      continue;
    }
    closeTable();
    if (t.startsWith('- ') || t.startsWith('* ')) {
      if (!inList) { html += '<ul style="margin:6px 0;padding-left:20px;">'; inList = true; }
      html += `<li>${inlineMd(t.slice(2))}</li>`;
      continue;
    }
    closeList();
    if (/^###\s+/.test(t)) { html += `<h3 style="margin:10px 0 4px;">${inlineMd(t.replace(/^###\s+/, ''))}</h3>`; }
    else if (/^##\s+/.test(t)) { html += `<h2 style="margin:12px 0 4px;">${inlineMd(t.replace(/^##\s+/, ''))}</h2>`; }
    else if (/^#\s+/.test(t)) { html += `<h1 style="margin:12px 0 4px;">${inlineMd(t.replace(/^#\s+/, ''))}</h1>`; }
    else { html += `<div style="margin:4px 0;">${inlineMd(t)}</div>`; }
  }
  closeList(); closeTable();
  return html;
}

// 通用展开栏：prefix 为页面标识，约定 md 路径为 /web/{prefix}_guide.md，元素 id 为 {prefix}GuideBody / {prefix}GuideToggleBtn
const _guideLoaded = {};

function toggleGuide(prefix) {
  const body = document.getElementById(prefix + 'GuideBody');
  const btn = document.getElementById(prefix + 'GuideToggleBtn');
  if (!body || !btn) return;
  const hidden = body.classList.toggle('hidden');
  btn.textContent = hidden ? '展开' : '收起';
  if (!hidden && !_guideLoaded[prefix]) {
    _guideLoaded[prefix] = true;
    body.innerHTML = '<p style="color:#999">加载中...</p>';
    fetch(`/web/${prefix}_guide.md`).then(r => r.text()).then(md => {
      body.innerHTML = renderMdSimple(md);
    }).catch(() => { body.innerHTML = '<p style="color:#c62828">使用说明加载失败</p>'; });
  }
}

function showLogin() { document.getElementById('loginOverlay').classList.remove('hidden'); }

function doLogin() {
  const pwd = document.getElementById('loginPassword').value;
  api('POST', '/api/login', { password: pwd }).then(data => {
    if (data.error) { alert(data.error); return; }
    document.getElementById('loginOverlay').classList.add('hidden');
    loadStatus();
    pollTasks();
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
  { id: 'index', href: '/web/index.html', label: '首页' },
  { id: 'dedup', href: '/web/dedup.html', label: '去重' },
  { id: 'organize', href: '/web/organize.html', label: '操作数据库' },
  { id: 'import', href: '/web/import.html', label: 'txt 批量写入' },
  { id: 'notebooks', href: '/web/notebooks.html', label: '笔记本管理' },
];

function injectNav(activeId) {
  const holder = document.getElementById('nav');
  if (!holder) return;
  const links = NAV_ITEMS.map(item =>
    `<a href="${item.href}" class="${item.id === activeId ? 'active' : ''}">${item.label}</a>`
  ).join('');
  holder.innerHTML = `<nav class="topnav">
    <div class="topnav-links">${links}</div>
    <div class="topnav-right">
      <button class="btn btn-outline" onclick="doRefresh()" title="重新发现笔记本">刷新</button>
      <button class="btn btn-outline" onclick="doRebuild()" title="增量重建所有笔记本索引">重建索引</button>
      <button class="btn btn-outline" onclick="doRebuildFull()" title="全量重构所有笔记本索引（换 embedding 模型后使用）">全量重构索引</button>
    </div>
  </nav>`;
}

// 全局导航右侧的索引操作按钮（所有页面可用）
function doRefresh() {
  api('POST', '/api/refresh').then(data => {
    if (data.error) { alert(data.error); return; }
    loadStatus();
    // 首页的浏览列表若展开则同步刷新
    const card = document.getElementById('notesCard');
    if (card && !card.classList.contains('hidden') && typeof fetchNotes === 'function') fetchNotes();
    alert('已刷新');
  }).catch(() => {});
}

function doRebuild() {
  if (!confirm('确认重建所有笔记本索引？可能需要一些时间。')) return;
  api('POST', '/api/rebuild').then(data => {
    if (data.error) { alert(data.error); return; }
    pollTasks();
  }).catch(() => {});
}

function doRebuildFull() {
  if (!confirm('确认全量重构所有笔记本索引？将忽略缓存、全部重新计算向量（换 embedding 模型后使用），可能需要较长时间。')) return;
  api('POST', '/api/rebuild', { force: true }).then(data => {
    if (data.error) { alert(data.error); return; }
    pollTasks();
  }).catch(() => {});
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
      <h3>请输入访问密码</h3>
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
      <strong>当前活跃任务</strong>
      <button class="btn btn-outline btn-sm" id="taskToggleBtn" onclick="toggleTaskCenter()">收起</button>
    </div>
    <div id="taskList" style="font-size: 13px;">加载中...</div>`;
  const container = document.querySelector('.container');
  if (container) {
    container.insertBefore(div, container.firstChild);
  } else {
    document.body.insertBefore(div, document.body.firstChild);
  }
}

function toggleTaskCenter() {
  const body = document.getElementById('taskList');
  const btn = document.getElementById('taskToggleBtn');
  if (!body || !btn) return;
  const hidden = body.classList.toggle('hidden');
  btn.textContent = hidden ? '展开' : '收起';
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
    body = `<span style="color:#4f9afe">${renderTaskProgress(t)}</span>`;
  } else if (t.status === 'done') {
    body = `<span style="color:#2e7d32">完成 — ${renderTaskSummary(t.result)}</span>`;
  } else if (t.status === 'interrupted') {
    const canResume = t.resume && t.resume.kind === 'txt_import';
    body = `<span style="color:#e65100">中断 — ${esc(t.error || '任务已中断')}</span>
      <span class="row" style="margin-top:6px;gap:8px;">
        ${canResume ? `<button class="btn btn-outline btn-sm" onclick="resumeTask('${esc(t.id)}')">再次尝试</button>` : ''}
        <button class="btn btn-outline btn-sm" onclick="cancelTask('${esc(t.id)}')">取消任务</button>
      </span>`;
  } else {
    body = `<span style="color:#c62828">失败 — ${esc(t.error || '未知错误')}</span>`;
  }
  return `<div style="padding:6px 0;border-bottom:1px solid #f0f0f0;">
    <strong>${esc(t.label || t.type)}</strong> ${body}
  </div>`;
}

function resumeTask(taskId) {
  api('POST', '/api/task/resume', { task_id: taskId }).then(data => {
    if (data.error) { alert(data.error); return; }
    pollTasks();
  }).catch(() => {});
}

function cancelTask(taskId) {
  if (!confirm('确认取消该任务并清空对应缓存？')) return;
  api('POST', '/api/task/cancel', { task_id: taskId }).then(data => {
    if (data.error) { alert(data.error); return; }
    pollTasks();
  }).catch(() => {});
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
    const hasActive = tasks.some(t => t.status === 'running' || t.status === 'interrupted');
    const prevRunning = document.getElementById('taskCenter').dataset.running === '1';
    list.innerHTML = tasks.map(renderTaskItem).join('');
    if (hasActive) {
      startTaskPoll();
    } else {
      stopTaskPoll();
    }
    document.getElementById('taskCenter').dataset.running = hasActive ? '1' : '0';
    // 任务从运行态变为结束态时刷新状态栏（笔记本数量/索引状态可能变化）
    if (prevRunning && !hasActive) loadStatus();
  }).catch(() => {
    const list = document.getElementById('taskList');
    if (list && list.textContent.includes('加载中')) {
      list.innerHTML = '<span style="color:#999">无法获取任务状态</span>';
    }
    stopTaskPoll();
  });
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
