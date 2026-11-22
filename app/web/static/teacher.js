const state = {children: [], books: [], session: null, turns: [], ws: null};
const VERDICT_PILL = {pass: 'ok', rewrite: 'warn', block: 'fail'};
const VERDICT_TEXT = {pass: '自检通过', rewrite: '自检已修正', block: '自检已拦截·降级'};

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function pill(status, text) {
  return `<span class="pill ${VERDICT_PILL[status] || status || ''}">${escapeHtml(text)}</span>`;
}

function checkTable(checks) {
  if (!checks || !checks.length) return '<p class="small muted">没有检查项</p>';
  const rows = checks.map((check) => `
    <tr>
      <td>${pill(check.status === 'fail' ? 'fail' : check.status === 'warn' ? 'warn' : 'ok', check.status)}</td>
      <td>${escapeHtml(check.name)}</td>
      <td>${escapeHtml(check.detail || '')}</td>
      <td>${escapeHtml(check.fix || '')}</td>
    </tr>`).join('');
  return `<table><tr><th>结论</th><th>检查项</th><th>说明</th><th>处理</th></tr>${rows}</table>`;
}

function gateBadge(gate, label) {
  if (!gate) return '';
  return `${pill(gate.verdict, `${label}${VERDICT_TEXT[gate.verdict] || gate.verdict} · ${gate.score}分`)}`;
}

// ---------------- 基础导航 ----------------

document.getElementById('tabs').addEventListener('click', (event) => {
  const tab = event.target.closest('.tab');
  if (!tab) return;
  document.querySelectorAll('.tab').forEach((item) => item.classList.toggle('active', item === tab));
  ['overview', 'profile', 'live', 'library', 'advice', 'reports', 'ops'].forEach((name) => {
    document.getElementById(`tab-${name}`).style.display = name === tab.dataset.tab ? '' : 'none';
  });
  if (tab.dataset.tab === 'profile') loadProfile();
});

async function logout() {
  await api('/api/logout', {method: 'POST', body: '{}'});
  location.href = '/';
}

function openModal(html) {
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal-mask').classList.add('open');
}

function closeModal() {
  document.getElementById('modal-mask').classList.remove('open');
}

document.getElementById('modal-mask').addEventListener('click', (event) => {
  if (event.target.id === 'modal-mask') closeModal();
});

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => toast('已复制链接')).catch(() => toast('复制失败，请手动选择'));
}

// ---------------- 概览 ----------------

async function loadOverview() {
  try {
    const data = await api('/api/teacher/overview');
    document.getElementById('kpi-pages').textContent = data.pages_today;
    document.getElementById('kpi-answered').textContent = data.answered_today != null
      ? `${data.answered_today} / ${data.pages_today} 页`
      : '0';
    document.getElementById('kpi-advice').textContent = data.pending_advice;
    document.getElementById('kpi-blocked').textContent = data.selfcheck_blocked;
    document.getElementById('sc-total').textContent = data.selfcheck.total;
    document.getElementById('sc-pass').textContent = data.selfcheck.passed;
    document.getElementById('sc-rewrite').textContent = data.selfcheck.rewritten;
    document.getElementById('sc-block').textContent = data.selfcheck.blocked;
    document.getElementById('sc-score').textContent = data.selfcheck.avg_score;
    document.getElementById('mt-calls').textContent = data.metrics.calls;
    document.getElementById('mt-latency').textContent = `${data.metrics.avg_latency} ms`;
    document.getElementById('mt-fail').textContent = `${data.metrics.failure_rate}%`;
    document.getElementById('mt-tokens').textContent = data.metrics.tokens;
    document.getElementById('mode-pills').innerHTML = Object.entries(data.modes)
      .map(([name, mode]) => `<span class="pill ${mode === 'stub' ? 'warn' : 'ok'}">${name}: ${mode}</span>`).join('');
    renderActiveSessions(data.active_sessions || []);
  } catch (error) {
    toast(error.message);
  }
}

// ---------------- 阅读档案 ----------------

async function loadProfile() {
  const select = document.getElementById('profile-child');
  if (!select.value && select.options.length) select.selectedIndex = 0;
  if (!select.value) {
    document.getElementById('pf-daily').innerHTML = '<p class="small muted">还没有孩子，先去「周报与授权」加入班级。</p>';
    return;
  }
  try {
    const data = await api(`/api/teacher/reading/${encodeURIComponent(select.value)}`);
    document.getElementById('pf-pages').textContent = data.pages;
    document.getElementById('pf-books').textContent = data.books_read;
    document.getElementById('pf-minutes').textContent = data.minutes;
    document.getElementById('pf-answered').textContent = data.pages ? `${data.answered_ratio}%` : '–';
    document.getElementById('pf-understood').textContent = data.pages ? `${data.understood_yes}/${data.pages}` : '–';
    document.getElementById('pf-free').textContent = data.free_questions;
    renderDaily(data.daily || []);
    document.getElementById('pf-books-list').innerHTML = (data.books || []).map((book) => `
      <div class="item">
        <div class="title">《${escapeHtml(book.title)}》</div>
        <div class="body">${book.pages} 页 · 最近一次 ${escapeHtml((book.last_at || '').slice(0, 16))}</div>
      </div>`).join('') || '<div class="empty">这本书还没有被读过。</div>';
    document.getElementById('pf-questions').innerHTML = (data.recent_questions || []).map((item) => `
      <div class="item">
        <div class="title">问：${escapeHtml(item.question)}</div>
        <div class="body">第 ${item.page_no} 页《${escapeHtml(item.book_title || '')}》 · 小读答：${escapeHtml(item.answer)}</div>
      </div>`).join('') || '<div class="empty">孩子还没主动提问过。共读时点【🙋 问一句】就能问。</div>';
  } catch (error) {
    toast(error.message);
  }
}

function renderDaily(rows) {
  const box = document.getElementById('pf-daily');
  const map = new Map((rows || []).map((row) => [row.day, row.pages]));
  const days = [];
  const today = new Date();
  for (let i = 13; i >= 0; i -= 1) {
    const d = new Date(today.getTime() - i * 86400000);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    days.push({label: `${d.getMonth() + 1}/${d.getDate()}`, pages: map.get(key) || 0});
  }
  const max = Math.max(1, ...days.map((day) => day.pages));
  const total = days.reduce((sum, day) => sum + day.pages, 0);
  box.innerHTML = `<div class="bars">${days.map((day) => `
    <div class="bar${day.pages ? '' : ' zero'}" title="${day.label} · ${day.pages} 页" style="height:${Math.max(3, Math.round(day.pages / max * 88))}px"></div>`).join('')}
  </div>
  <div class="small muted" style="margin-top:6px">${days[0].label} → ${days[days.length - 1].label} · 共 ${total} 页</div>`;
}

function renderActiveSessions(items) {
  const box = document.getElementById('active-sessions');
  if (!items.length) {
    box.innerHTML = '<div class="empty">现在没有进行中的共读。去「开始共读」吧。</div>';
    return;
  }
  box.innerHTML = items.map((item) => `
    <div class="item">
      <div class="title">${escapeHtml(item.child_name)} · 《${escapeHtml(item.book_title)}》 ${pill('ok', item.state_label || '')}</div>
      <div class="body">第 ${item.last_page_no || 0} 页 · 开始于 ${escapeHtml(item.created_at)}</div>
      <div class="actions"><button class="btn small" onclick="attachSession('${item.id}')">查看会话</button></div>
    </div>`).join('');
}

// ---------------- 开始共读 ----------------

async function loadChildren() {
  const data = await api('/api/teacher/children');
  state.children = data.items;
  document.getElementById('live-child').innerHTML = data.items
    .map((child) => `<option value="${child.id}">${escapeHtml(child.name)} · ${escapeHtml(child.age_label)}</option>`).join('');
  const list = document.getElementById('children-list');
  const profileSelect = document.getElementById('profile-child');
  if (profileSelect) {
    profileSelect.innerHTML = data.items
      .map((child) => `<option value="${child.id}">${escapeHtml(child.name)} · ${escapeHtml(child.age_label)}</option>`).join('');
  }
  list.innerHTML = data.items.map((child) => `
    <div class="item">
      <div class="title">${escapeHtml(child.name)}
        ${child.guardian_consent ? pill('ok', '已授权') : pill('warn', '未授权')}
        <span class="small muted">${escapeHtml(child.age_label)} · ${escapeHtml(child.class_name)}</span>
      </div>
      <div class="actions">
        <button class="btn ghost small" onclick="toggleConsent(${child.id}, ${child.guardian_consent ? 'false' : 'true'})">
          ${child.guardian_consent ? '标记为未授权' : '登记授权'}
        </button>
        <button class="btn ghost small" onclick="generateWeekly(${child.id})">生成周报</button>
        <a class="btn ghost small" href="/parent/${child.parent_token}" target="_blank">家长链接</a>
        ${window.TEACHER_ROLE === 'admin'
          ? `<button class="btn ghost small" onclick="deleteChild(${child.id}, '${escapeHtml(child.name)}')">删除全部记录</button>`
          : ''}
      </div>
    </div>`).join('') || '<div class="empty">还没有孩子，先在下面加入班级。</div>';
}

async function loadBooks() {
  const data = await api('/api/teacher/books');
  state.books = data.items;
  document.getElementById('live-book').innerHTML = data.items
    .map((book) => `<option value="${book.id}">《${escapeHtml(book.title)}》 · ${book.page_count} 页</option>`).join('') ||
    '<option value="">还没有绘本</option>';
  document.getElementById('upload-book').innerHTML = document.getElementById('live-book').innerHTML;
  document.getElementById('bank-book').innerHTML = document.getElementById('live-book').innerHTML;
  renderBooks();
  if (data.items.length) loadBank();
}

function renderBooks() {
  const box = document.getElementById('book-list');
  if (!state.books.length) {
    box.innerHTML = '<div class="empty">还没有绘本。先在右边新建一本，再上传页面。</div>';
    return;
  }
  box.innerHTML = state.books.map((book) => `
    <div class="item">
      <div class="title">《${escapeHtml(book.title)}》
        <span class="pill">${book.language === 'en' ? '英文' : '中文'}</span>
        <span class="small muted">${book.page_count} 页 · ${escapeHtml(book.theme || '')}</span>
      </div>
      <div class="row" style="margin-top:10px;gap:10px">
        ${book.pages.map((page) => `
          <div style="text-align:center;cursor:pointer" onclick="editPage(${page.id})">
            ${page.image ? `<img class="thumb" src="${page.image}" alt="第${page.page_no}页">` : '<div class="thumb"></div>'}
            <div class="small muted">第 ${page.page_no} 页</div>
            <div>${page.status === 'ready' ? pill('ok', '可用') : pill('warn', '待校对')}</div>
          </div>`).join('') || '<div class="empty">还没有页面</div>'}
      </div>
    </div>`).join('');
}

async function startSession() {
  const childId = Number(document.getElementById('live-child').value);
  const bookId = Number(document.getElementById('live-book').value);
  if (!childId || !bookId) { toast('先选孩子和绘本'); return; }
  try {
    const data = await api('/api/teacher/sessions', {
      method: 'POST', body: JSON.stringify({child_id: childId, book_id: bookId}),
    });
    const url = `${location.origin}${data.robot_url}`;
    document.getElementById('session-start').style.display = '';
    document.getElementById('session-start').innerHTML = `
      <div class="qr-box" style="margin-top:14px">
        <div>
          <div class="small muted">在机器人 / 平板上打开这个链接</div>
          <div class="url">${escapeHtml(url)}</div>
          <div class="row" style="margin-top:8px">
            <a class="btn small" href="${url}" target="_blank">打开机器人端</a>
            <button class="btn ghost small" onclick="copyText('${url}')">复制链接</button>
          </div>
        </div>
      </div>`;
    state.session = {session_id: data.session_id, token: data.token};
    toast('共读已开始，等待设备翻页');
    await attachSession(data.session_id);
    loadOverview();
  } catch (error) {
    toast(error.message);
  }
}

async function attachSession(sessionId) {
  const data = await api(`/api/teacher/sessions/${sessionId}`);
  state.session = {session_id: sessionId};
  state.turns = data.turns || [];
  document.getElementById('live-state').textContent = data.snapshot ? data.snapshot.state_label : '已结束';
  document.getElementById('live-head').innerHTML =
    `${escapeHtml(data.child.name)} · 《${escapeHtml(data.book.title)}》 · 共 ${data.book ? '' : ''}${state.turns.length} 条记录`;
  renderTimeline();
  if (data.snapshot && data.snapshot.current) renderStage(data.snapshot.current);
  loadOverview();
}

function renderTimeline() {
  const box = document.getElementById('live-timeline');
  if (!state.turns.length) {
    box.innerHTML = '<div class="empty">还没有问答记录。设备拍下第一页后会自动出现。</div>';
    return;
  }
  box.innerHTML = state.turns.map((turn) => {
    const checker = turn.checker || {};
    const gate = checker.question || checker;
    const verdict = gate.verdict || checker.verdict || 'pass';
    return `
      <div class="turn">
        <div class="q">第 ${turn.page_no} 页 · ${escapeHtml(turn.question)}</div>
        <div class="a">${turn.answer_text ? `孩子说：${escapeHtml(turn.answer_text)}` : '（还没有回答）'}
          ${turn.understood ? ` · 理解：${escapeHtml(turn.understood)}` : ''}</div>
        <div class="checks">
          ${pill(verdict, VERDICT_TEXT[verdict] || verdict)}
          <span class="small muted">${gate.score != null ? gate.score + ' 分' : ''} ${checker.source ? '· 来源 ' + escapeHtml(checker.source) : ''}${turn.latency_ms ? ' · 本页 ' + (turn.latency_ms / 1000).toFixed(1) + ' 秒' : ''}</span>
          <details><summary>检查项</summary>${checkTable(gate.checks || checker.checks || [])}</details>
        </div>
      </div>`;
  }).join('');
}

function renderStage(current) {
  const box = document.getElementById('live-stage');
  const question = current.question || {};
  const checker = current.checker || {};
  box.innerHTML = `
    <div class="row" style="align-items:flex-start;gap:14px">
      <div style="flex:1">
        <div class="small muted">第 ${current.page_no} 页 · 忠实描述</div>
        <p style="margin:6px 0 12px">${escapeHtml((current.summary && current.summary.faithful_description) || '')}</p>
        <div class="small muted">可朗读讲稿</div>
        <p style="margin:6px 0 12px">${escapeHtml((current.summary && current.summary.read_aloud_script) || '')}</p>
        <div class="small muted">提问</div>
        <p style="font-weight:600">${escapeHtml(question.question || '')}</p>
        <div class="row" style="margin-top:8px">
          ${gateBadge(checker.question, '提问')}
          ${gateBadge(checker.summary, '摘要')}
        </div>
      </div>
    </div>`;
}

async function control(action) {
  if (!state.session) { toast('先开始一次共读'); return; }
  try {
    await api(`/api/device/${state.session.session_id}/control`, {
      method: 'POST', body: JSON.stringify({action, token: state.session.token || ''}),
    });
    if (action === 'end') await endSession();
  } catch (error) {
    toast(error.message);
  }
}

async function endSession() {
  if (!state.session) return;
  try {
    const data = await api(`/api/teacher/sessions/${state.session.session_id}/end`, {method: 'POST', body: '{}'});
    toast(`已生成 ${data.advice ? data.advice.length : 0} 条教学建议`);
    loadAdvice(); loadReports(); loadOverview();
  } catch (error) {
    toast(error.message);
  }
}

// ---------------- 实时事件 ----------------

function connectWS() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${scheme}://${location.host}/ws/teacher`);
  state.ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === 'snapshot') return;
    if (message.type !== 'event') return;
    if (!state.session || message.session_id !== state.session.session_id) {
      if (message.event === 'ended') loadOverview();
      return;
    }
    handleEvent(message);
  };
  state.ws.onclose = () => setTimeout(connectWS, 2500);
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => attachSession(state.session.session_id), 350);
}

function handleEvent(message) {
  const data = message.data || {};
  if (data.state_label) document.getElementById('live-state').textContent = data.state_label;
  if (message.event === 'page') {
    renderStage({
      page_no: data.page_no, summary: data.display, question: data.question,
      checker: data.checker, facts: data.facts,
    });
    scheduleRefresh();
  } else if (message.event === 'ask') {
    toast('机器人开始提问了');
    scheduleRefresh();
  } else if (message.event === 'reply') {
    document.getElementById('live-head').innerHTML =
      `孩子说：${escapeHtml(data.answer_text || '（没有听到）')} · 机器人回应：${escapeHtml(data.reply || '')}`;
    scheduleRefresh();
  } else if (message.event === 'quality') {
    toast(data.human_hint || '照片需要重拍');
  } else if (message.event === 'timeout') {
    toast(data.human_hint || '孩子没有回答');
  } else if (message.event === 'free') {
    document.getElementById('live-head').innerHTML =
      `孩子问：${escapeHtml(data.question || '（没听清）')} · 小读答：${escapeHtml(data.answer || '')}`;
    scheduleRefresh();
  } else if (message.event === 'eye_rest') {
    toast(data.human_hint || '该歇眼睛了');
    scheduleRefresh();
  } else if (message.event === 'ended') {
    toast('这次共读结束了');
    document.getElementById('live-state').textContent = '已结束';
    state.session = null;
    loadAdvice(); loadReports(); loadOverview();
  } else if (message.event === 'error') {
    toast(data.human_hint || '这一步没成功');
  }
}

// ---------------- 绘本库 ----------------

async function createBook() {
  const title = document.getElementById('book-title').value.trim();
  if (!title) { toast('先填书名'); return; }
  try {
    await api('/api/teacher/books', {
      method: 'POST',
      body: JSON.stringify({
        title,
        language: document.getElementById('book-language').value,
        theme: document.getElementById('book-theme').value,
      }),
    });
    toast('绘本已创建');
    await loadBooks();
  } catch (error) { toast(error.message); }
}

let pickedPageBlob = null;  // 拍照/截图/粘贴得到的图，优先于文件选择框

/* 手选文件时丢掉之前从面板取的图，否则会一直上传旧图 */
document.getElementById('upload-file').addEventListener('change', () => {
  if (pickedPageBlob) {
    pickedPageBlob = null;
    const note = document.getElementById('upload-picked');
    if (note) note.textContent = '已选择本地文件，上传时会用它。';
  }
});

async function pickUploadPhoto() {
  if (!window.ImageSource) { toast('这个浏览器不支持，请用下面的【选择文件】'); return; }
  const shot = await ImageSource.pick({title: '页面照片', hint: '摄像头拍、网络摄像头、本地图片、屏幕截图、粘贴任选一种'});
  if (!shot) return;
  pickedPageBlob = shot.blob;
  const fileInput = document.getElementById('upload-file');
  fileInput.value = '';
  const note = document.getElementById('upload-picked');
  const label = {camera: '摄像头', net: '网络摄像头', file: '本地图片', screen: '屏幕截图', paste: '剪贴板'}[shot.source] || '图片';
  note.innerHTML = `已取得${label}：<img src="${shot.dataUrl}" alt="待上传的页面" style="height:72px;border-radius:10px;vertical-align:middle;margin-left:6px">`;
}

async function uploadPage() {
  const bookId = document.getElementById('upload-book').value;
  const pageNo = Number(document.getElementById('upload-page-no').value || 1);
  const fileInput = document.getElementById('upload-file');
  const file = pickedPageBlob || (fileInput.files && fileInput.files[0]);
  if (!bookId || !file) { toast('选一本绘本和一张照片'); return; }
  const form = new FormData();
  form.append('page_no', pageNo);
  form.append('file', file, file.name || 'page.jpg');
  try {
    const response = await fetch(`/api/teacher/books/${bookId}/pages`, {method: 'POST', body: form});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '上传失败');
    toast(data.human_hint || '已上传');
    fileInput.value = '';
    pickedPageBlob = null;
    const note = document.getElementById('upload-picked');
    if (note) note.textContent = '识别草稿需要老师校对后才会被机器人使用。';
    await loadBooks();
    if (data.page_id) editPage(data.page_id);
  } catch (error) { toast(error.message); }
}

async function editPage(pageId) {
  let full = null;
  try {
    const data = await api(`/api/teacher/pages/${pageId}`);
    full = data.page;
  } catch (error) {
    toast(error.message);
    return;
  }
  openModal(`
    <h3>第 ${full.page_no} 页 · 校对三层文本</h3>
    <p class="small muted">原文层必须来自 OCR；描述只写画面里看得见的东西。保存后机器人才会使用。</p>
    <div class="field"><label>原文（OCR）</label><textarea id="edit-raw">${escapeHtml(full.raw_text || '')}</textarea></div>
    <div class="field"><label>忠实描述</label><textarea id="edit-desc">${escapeHtml(full.faithful_description || '')}</textarea></div>
    <div class="field"><label>可朗读讲稿</label><textarea id="edit-script">${escapeHtml(full.read_aloud_script || '')}</textarea></div>
    <div class="field"><label>关键词（逗号分隔）</label><input id="edit-keywords" type="text" value="${escapeHtml((full.keywords || []).join('，'))}"></div>
    <div class="row" style="justify-content:flex-end">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn" onclick="savePage(${pageId})">保存</button>
    </div>`);
}

async function savePage(pageId) {
  try {
    await api(`/api/teacher/pages/${pageId}`, {
      method: 'PATCH',
      body: JSON.stringify({
        raw_text: document.getElementById('edit-raw').value,
        faithful_description: document.getElementById('edit-desc').value,
        read_aloud_script: document.getElementById('edit-script').value,
        keywords: document.getElementById('edit-keywords').value.split(/[，,]/).map((item) => item.trim()).filter(Boolean),
      }),
    });
    closeModal();
    toast('已保存，下一轮共读就会用上');
    await loadBooks();
  } catch (error) { toast(error.message); }
}

async function loadBank() {
  const bookId = document.getElementById('bank-book').value;
  if (!bookId) return;
  renderBankPages(bookId);
  const data = await api(`/api/teacher/bank?book_id=${bookId}`);
  const box = document.getElementById('bank-list');
  box.innerHTML = data.items.map((item) => `
    <div class="item">
      <div class="title">${escapeHtml(item.text)} <span class="pill">${escapeHtml(item.qtype)}</span></div>
      <div class="body">第 ${item.page_no} 页 · ${item.source === 'seed' ? '示例' : item.source}</div>
      <div class="actions"><button class="btn ghost small" onclick="removeBank(${item.id})">停用</button></div>
    </div>`).join('') || '<div class="empty">这本绘本还没有题目，机器人会现场生成。</div>';
}

function renderBankPages(bookId) {
  const book = state.books.find((item) => String(item.id) === String(bookId));
  const pages = (book && book.pages) || [];
  document.getElementById('bank-page').innerHTML = pages
    .map((page) => `<option value="${page.id}">第 ${page.page_no} 页${page.status === 'ready' ? '' : '（待校对）'}</option>`)
    .join('') || '<option value="">还没有页面</option>';
}

async function addBank() {
  const bookId = document.getElementById('bank-book').value;
  const pageId = Number(document.getElementById('bank-page').value || 0);
  const text = document.getElementById('bank-text').value.trim();
  if (!bookId || !text) { toast('先选绘本并填写题目'); return; }
  if (!pageId) { toast('这本绘本还没有页面，先上传页面'); return; }
  try {
    await api('/api/teacher/bank', {
      method: 'POST',
      body: JSON.stringify({book_page_id: pageId, text, qtype: document.getElementById('bank-type').value}),
    });
    document.getElementById('bank-text').value = '';
    toast('已加入题库');
    loadBank();
  } catch (error) { toast(error.message); }
}

async function removeBank(itemId) {
  await api(`/api/teacher/bank/${itemId}`, {method: 'DELETE'});
  toast('已停用');
  loadBank();
}

// ---------------- 建议 ----------------

async function loadAdvice() {
  const data = await api('/api/teacher/advice');
  const box = document.getElementById('advice-list');
  if (!data.items.length) {
    box.innerHTML = '<div class="empty">还没有建议。结束一次共读后会自动生成，每条都带依据。</div>';
    return;
  }
  box.innerHTML = data.items.map((item) => `
    <div class="item">
      <div class="title">${escapeHtml(item.child_name)}
        ${item.status === 'pending' ? pill('warn', '待确认') : item.status === 'accepted' ? pill('ok', '已采纳') : pill('', '已忽略')}
        <span class="small muted">依据：问答 #${(item.evidence || []).join('、#')}</span>
      </div>
      <div class="body">${escapeHtml(item.edited_text || item.text)}</div>
      <div class="actions">
        <button class="btn small" onclick="decideAdvice(${item.id}, 'accept')">采纳</button>
        <button class="btn ghost small" onclick="editAdvice(${item.id}, '${escapeHtml(item.text).replace(/'/g, '’')}')">修改</button>
        <button class="btn ghost small" onclick="decideAdvice(${item.id}, 'ignore')">忽略</button>
      </div>
    </div>`).join('');
}

async function decideAdvice(adviceId, action, text) {
  try {
    await api(`/api/teacher/advice/${adviceId}/decide`, {method: 'POST', body: JSON.stringify({action, text: text || ''})});
    if (text) closeModal();
    toast(action === 'ignore' ? '已忽略' : '已采纳');
    loadAdvice();
  } catch (error) { toast(error.message); }
}

function editAdvice(adviceId, text) {
  openModal(`
    <h3>修改建议</h3>
    <p class="small muted">修改后的版本会覆盖原建议，审计痕迹保留。</p>
    <div class="field"><textarea id="advice-edit">${escapeHtml(text)}</textarea></div>
    <div class="row" style="justify-content:flex-end">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn" onclick="decideAdvice(${adviceId}, 'edit', document.getElementById('advice-edit').value)">保存</button>
    </div>`);
}

// ---------------- 周报与授权 ----------------

async function loadReports() {
  const data = await api('/api/teacher/reports');
  const box = document.getElementById('report-list');
  box.innerHTML = data.items.map((item) => `
    <div class="item">
      <div class="title">${escapeHtml(item.child_name)} · ${escapeHtml(item.week_start)} 至 ${escapeHtml(item.week_end)}
        ${item.status === 'published' ? pill('ok', '已发布') : pill('warn', '草稿')}</div>
      <div class="body">${escapeHtml((item.payload.headline) || '')}</div>
      <div class="actions">
        ${item.status === 'published' ? '' : `<button class="btn small" onclick="publishReport(${item.id})">确认发布给家长</button>`}
        <a class="btn ghost small" href="/parent/${item.parent_token}" target="_blank">家长卡片</a>
      </div>
    </div>`).join('') || '<div class="empty">还没有周报。先给孩子生成一份。</div>';
}

async function generateWeekly(childId) {
  try {
    const data = await api(`/api/teacher/weekly/${childId}`, {method: 'POST', body: '{}'});
    toast(`周报已生成，自检 ${data.selfcheck.score} 分`);
    loadReports();
  } catch (error) { toast(error.message); }
}

async function publishReport(reportId) {
  await api(`/api/teacher/reports/${reportId}/publish`, {method: 'POST', body: '{}'});
  toast('已发布，家长可以看到');
  loadReports();
}

async function toggleConsent(childId, consented) {
  try {
    await api(`/api/teacher/children/${childId}/consent`, {
      method: 'POST', body: JSON.stringify({consented}),
    });
    toast(consented ? '已登记授权' : '已标记为未授权');
    loadChildren();
  } catch (error) { toast(error.message); }
}

async function createChild() {
  const name = document.getElementById('child-name').value.trim();
  if (!name) { toast('先填名字'); return; }
  try {
    await api('/api/teacher/children', {
      method: 'POST',
      body: JSON.stringify({name, age_years: document.getElementById('child-age').value}),
    });
    document.getElementById('child-name').value = '';
    toast('已加入班级');
    loadChildren();
  } catch (error) { toast(error.message); }
}

async function deleteChild(childId, name) {
  if (!confirm(`删除 ${name} 的全部记录？这会同时删除问答、建议与周报，且不可恢复。`)) return;
  try {
    const data = await api(`/api/teacher/children/${childId}`, {method: 'DELETE'});
    toast(`已删除 ${data.deleted_sessions || 0} 次共读记录`);
    loadChildren(); loadOverview();
  } catch (error) { toast(error.message); }
}

// ---------------- 自检与运维 ----------------

async function runSystemCheck(renderTable) {
  try {
    const data = await api('/api/teacher/selfcheck/system');
    const failed = data.checks.filter((check) => check.status === 'fail');
    const warned = data.checks.filter((check) => check.status === 'warn');
    const strip = document.getElementById('selfcheck-strip');
    if (failed.length) {
      strip.style.display = '';
      strip.className = 'demo-banner';
      strip.style.background = '#fdeceb';
      strip.style.borderColor = '#f3cfcc';
      strip.innerHTML = `<strong>自检未通过：</strong>${failed.map((check) => escapeHtml(check.name + '：' + (check.detail || '') + ' → ' + (check.fix || ''))).join('；')}`;
    } else if (warned.length) {
      strip.style.display = '';
      strip.className = 'demo-banner';
      strip.style.background = '#fff5e6';
      strip.style.borderColor = '#f5d9b3';
      strip.innerHTML = `<strong>可以开始共读。</strong>${warned.map((check) => escapeHtml(check.name + '：' + (check.detail || ''))).join('；')}`;
    } else {
      strip.style.display = 'none';
    }
    if (renderTable) {
      document.getElementById('system-check').innerHTML = `
        <div class="row" style="margin-bottom:8px">${pill(data.verdict, data.summary)} <span class="small muted">${escapeHtml(data.checked_at || '')}</span></div>
        ${checkTable(data.checks)}`;
    }
    return data;
  } catch (error) {
    toast(error.message);
  }
}

async function runDeepCheck() {
  const box = document.getElementById('deep-check');
  box.innerHTML = '<p class="small muted">正在真跑五个引擎：OCR → VLM → LLM → TTS → ASR，约 20-40 秒，先别关页面…</p>';
  try {
    const data = await api('/api/teacher/selfcheck/deep', {method: 'POST'});
    if (data.verdict === 'busy') {
      box.innerHTML = `<p class="small muted">${escapeHtml(data.summary || '上一次还在跑')}</p>`;
      return;
    }
    const seconds = data.total_ms ? `共 ${(data.total_ms / 1000).toFixed(1)} 秒 · ` : '';
    box.innerHTML = `
      <div class="row" style="margin-bottom:8px">${pill(data.verdict === 'pass' ? 'ok' : 'fail', data.verdict === 'pass' ? '全部跑通' : '没跑通')}
        <span class="small muted">${seconds}${escapeHtml(data.checked_at || '')}</span></div>
      <p class="small muted">${escapeHtml(data.summary || '')}</p>${deepTable(data.checks)}`;
    if (data.verdict !== 'pass') toast(data.summary || '深度自检没通过');
  } catch (error) {
    box.innerHTML = `<p class="small muted">没跑起来：${escapeHtml(error.message || '')}</p>`;
    toast(error.message);
  }
}

function deepTable(checks) {
  if (!checks || !checks.length) return '<p class="small muted">没有检查项</p>';
  const rows = checks.map((check) => `
    <tr>
      <td>${pill(check.status === 'fail' ? 'fail' : check.status === 'warn' ? 'warn' : 'ok', check.status)}</td>
      <td>${escapeHtml(check.name)}</td>
      <td>${escapeHtml(check.detail || '')}</td>
      <td class="mono">${check.ms || 0} ms</td>
      <td>${escapeHtml(check.fix || '')}</td>
    </tr>`).join('');
  return `<table><tr><th>结论</th><th>引擎</th><th>实测</th><th>耗时</th><th>处理</th></tr>${rows}</table>`;
}

async function runDemo() {
  try {
    const data = await api('/api/teacher/selfcheck/demo');
    document.getElementById('demo-check').innerHTML = data.cases.map((item) => `
      <div class="item">
        <div class="title">${escapeHtml(item.name)} ${pill(item.report.verdict === 'pass' ? 'ok' : item.report.verdict === 'rewrite' ? 'warn' : 'fail', item.report.verdict)}
          <span class="small muted">${item.report.score} 分</span></div>
        <div class="body">输入：${escapeHtml(item.input)}</div>
        <div class="body">结论：${escapeHtml(item.report.summary || '')}</div>
        <details><summary class="small muted">检查明细</summary>${checkTable(item.report.checks)}</details>
      </div>`).join('');
  } catch (error) { toast(error.message); }
}

async function loadHistory() {
  try {
    const data = await api('/api/teacher/selfcheck/history');
    const rows = data.items.map((item) => `
      <tr>
        <td>${pill(item.verdict === 'pass' ? 'ok' : item.verdict === 'rewrite' ? 'warn' : 'fail', item.verdict)}</td>
        <td>${escapeHtml(item.scope)}</td>
        <td class="mono">${escapeHtml(item.target || '')}</td>
        <td>${item.score}</td>
        <td class="small muted">${escapeHtml(item.created_at)}</td>
      </tr>`).join('');
    document.getElementById('check-history').innerHTML =
      `<table class="table"><tr><th>结论</th><th>范围</th><th>目标</th><th>分数</th><th>时间</th></tr>${rows}</table>`;
  } catch (error) { toast(error.message); }
}

async function loadMetrics() {
  try {
    const data = await api('/api/teacher/metrics');
    const rows = (data.slowest || []).map((item) => `
      <tr><td class="mono">${escapeHtml(item.session_id).slice(0, 8)}</td><td>${item.page_no}</td>
      <td>${escapeHtml(item.stage)}</td><td>${Math.round(item.latency_ms)} ms</td></tr>`).join('');
    document.getElementById('slow-pages').innerHTML = rows
      ? `<table class="table"><tr><th>会话</th><th>页</th><th>环节</th><th>耗时</th></tr>${rows}</table>`
      : '<div class="empty">今天还没有调用记录</div>';
  } catch (error) { toast(error.message); }
}

async function runAudit() {
  const box = document.getElementById('audit-result');
  try {
    const data = await api('/api/teacher/audit');
    const bad = data.items.filter((item) => !item.allowed);
    box.innerHTML = `<strong>${escapeHtml(data.human_hint)}</strong>` +
      (bad.length ? `<br>${bad.map((item) => `${escapeHtml(item.host)}：${escapeHtml(item.reason)}`).join('<br>')}` : '');
  } catch (error) {
    box.textContent = error.message;
  }
}

// ---------------- 启动 ----------------

(async function boot() {
  try {
    await Promise.all([loadOverview(), loadChildren(), loadBooks(), loadAdvice(), loadReports()]);
    connectWS();
    runSystemCheck(false);
    loadHistory();
    loadMetrics();
    loadProfile();
  } catch (error) {
    toast(error.message);
  }
})();