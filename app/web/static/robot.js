/* 机器人/平板端：一个大按钮 + 大字号 + 状态必须写清楚（规范 §9.3）。
 *
 * 园里大部分时间由机器人承担，所以可用性按第一优先级做：
 * - WebSocket 为主通道，15s 一次心跳；25s 没 pong 就主动重连（半死连接能发现）；
 * - 连续 3 次连不上，自动降级成 HTTP 兼容模式：上传/回答走 POST，界面 2s 拉一次 snapshot；
 * - 断线时主按钮置灰并写「正在重连…」，恢复后按服务端 snapshot 复原；
 * - 识别超过 25s 没有任何结果，提示重拍，不让界面卡死在「识别中…」；
 * - 4401（凭证失效）/ 4404（共读已结束）停止重连，直接告诉人该找老师。
 */
const R = window.ROBOT;
const stage = document.getElementById('stage');
const stateText = document.getElementById('state-text');
const mainBtn = document.getElementById('main-btn');
const choicesBox = document.getElementById('choices');
const answerBar = document.getElementById('answer-bar');
const answerInput = document.getElementById('answer-input');
const conn = document.getElementById('conn');
const connText = document.getElementById('conn-text');
const camera = document.getElementById('camera');
const micBtn = document.getElementById('mic-btn');
const fsBtn = document.getElementById('fs-btn');
const freeBtn = document.getElementById('free-btn');
const guardBtn = document.getElementById('guard-btn');
const guardVideo = document.getElementById('guard-video');

const HEARTBEAT_MS = 15000;
const PONG_TIMEOUT_MS = 25000;
const ANALYZE_TIMEOUT_MS = 25000;
const POLL_MS = 2000;
const WS_MAX_FAILS = 3;

let phase = 'idle';       // idle | busy | asked | answered | ended
let nextPage = 1;
let currentQuestion = null;
let transport = 'ws';     // ws | http
let ws = null;
let wsFails = 0;
let lastPong = 0;
let fatal = false;
let heartbeatTimer = null;
let analyzeTimer = null;
let pollTimer = null;
let renderedKey = '';     // 防止轮询模式下每 2s 重绘/重播
let lastThumb = null;     // 最近一页的小图：提问/回答时留个画面参照，别让绘本页凭空消失
let talkMode = 'answer';  // answer = 回答提问，free = 孩子自己提问
let guard = null;         // 护眼检测：{stream, detector, timer, hits, lastAt}

function toast(text) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(window.__t);
  window.__t = setTimeout(() => el.classList.remove('show'), 2600);
}

function setState(label, live) {
  stateText.textContent = label;
  document.getElementById('state-pill').classList.toggle('live', !!live);
}

function setMain(label, enabled) {
  mainBtn.textContent = label;
  mainBtn.disabled = !enabled;
}

function setConn(text, on) {
  conn.classList.toggle('on', !!on);
  connText.textContent = text;
}

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let currentAudio = null;  // 同一时刻只播一句：讲稿和提问不能叠在一起

function playAudio(url, fallbackHint) {
  if (!url) return;
  if (currentAudio) {
    try { currentAudio.pause(); } catch (error) { /* 已播完 */ }
  }
  const audio = new Audio(url);
  currentAudio = audio;
  audio.play().catch(() => {
    toast(fallbackHint || '点一下【再听一遍】就能听到提问');
  });
}

function stopAudio() {
  if (!currentAudio) return;
  try { currentAudio.pause(); } catch (error) { /* 已播完 */ }
  currentAudio = null;
}

function showStory(text) {
  const box = document.getElementById('story');
  if (!box) return;
  box.textContent = text || '';
  box.hidden = !text;
}

function showRest(text) {
  const box = document.getElementById('rest');
  if (!box) return;
  box.textContent = text || '';
  box.hidden = !text;
}

/* 孩子自己提问：借同一个输入条，只换语义；不新开一套控件，机器人上少一个要认的按钮 */
function enterFree() {
  if (fatal || phase === 'ended') return;
  if (phase === 'busy') {
    toast('等这一页看完再问');
    return;
  }
  talkMode = 'free';
  answerBar.style.display = 'flex';
  answerInput.placeholder = '把小朋友的问题打在这里，比如：小兔子为什么跑？';
  setTalkLabel('🎤 按住问');
  setState('我在听你问', true);
  if (micBtn && !micBtn.hidden) toast('按住麦克风问，或者打字');
  else answerInput.focus();
}

function exitFree() {
  if (talkMode !== 'free') return;
  talkMode = 'answer';
  answerBar.style.display = phase === 'asked' ? 'flex' : 'none';
  answerInput.placeholder = '也可以把小朋友说的话打在这里';
  setTalkLabel('🎤 按住说话');
}

// ---------------- 渲染 ----------------

function pictureRef() {
  if (!lastThumb || !lastThumb.b64) return '';
  return `<img class="pic-side" alt="这一页" src="data:${lastThumb.mime};base64,${lastThumb.b64}">`;
}

/* 有图就把图放左边、问题放右边；没有图就维持原样 */
function askLayout(inner) {
  const ref = pictureRef();
  return ref ? `<div class="ask-layout">${ref}<div class="ask-main">${inner}</div></div>` : inner;
}

function renderPicture(payload) {
  const thumb = payload && payload.thumbnail;
  if (thumb && thumb.b64) lastThumb = thumb;
  const placeholder = `<div class="placeholder">${payload && payload.display && payload.display.description
    ? escapeHtml(payload.display.description)
    : '拍一页，我就开始讲故事'}</div>`;
  if (thumb && thumb.b64) {
    stage.innerHTML = `<img class="pic" alt="这一页" src="data:${thumb.mime};base64,${thumb.b64}">`;
  } else {
    stage.innerHTML = placeholder;
  }
}

function renderQuestion(question, hint) {
  currentQuestion = question || null;
  const text = (question && question.question) || '';
  const choices = (question && question.choices) || [];
  stage.innerHTML = askLayout(`
    ${text ? `<div class="question">${escapeHtml(text)}</div>` : ''}
    ${hint ? `<div class="hint">${escapeHtml(hint)}</div>` : ''}
    ${choices.length ? '' : '<div class="hint">听完可以说一说，也可以点下面的图片</div>'}`);
  choicesBox.innerHTML = choices.map((item) =>
    `<button class="choice" data-answer="${escapeHtml(item)}">${escapeHtml(item)}</button>`).join('');
}

function renderReply(data) {
  stage.innerHTML = askLayout(`
    <div class="answer">小朋友：${escapeHtml(data.answer_text || '（没有听清）')}</div>
    <div class="question" style="font-size:24px">${escapeHtml(data.reply || '')}</div>`);
  choicesBox.innerHTML = '';
  answerBar.style.display = 'none';
  answerInput.value = '';
  cancelTalk();
  exitFree();
}

/* 孩子自由提问的回答：问题与回答一起上屏，仍然保留这一页的画面 */
function renderFree(data) {
  const question = data.question ? `小朋友问：${escapeHtml(data.question)}` : '（这次没听清）';
  stage.innerHTML = askLayout(`
    <div class="answer">${question}</div>
    <div class="question" style="font-size:22px">${escapeHtml(data.answer || '')}</div>`);
  choicesBox.innerHTML = '';
}

// ---------------- 连接：WS 为主，HTTP 兜底 ----------------

function connect() {
  if (fatal || transport !== 'ws') return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${scheme}://${location.host}/ws/device/${R.sessionId}?token=${encodeURIComponent(R.token)}`;
  setConn(wsFails ? '重连中…' : '连接中…', false);
  let socket;
  try {
    socket = new WebSocket(url);
  } catch (error) {
    return fallback();
  }
  ws = socket;
  socket.onopen = () => {
    if (socket !== ws) return;
    wsFails = 0;
    lastPong = Date.now();
    setConn('已连接', true);
    startHeartbeat();
  };
  socket.onerror = () => { /* 交给 onclose 统一处理 */ };
  socket.onmessage = (event) => {
    if (socket !== ws) return;
    let message = null;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    if (message.type === 'pong') {
      lastPong = Date.now();
      return;
    }
    if (message.type === 'snapshot') {
      applySnapshot(message.data);
      return;
    }
    if (message.type === 'error') {
      handleServerError(message);
      return;
    }
    if (message.type === 'event') handleEvent(message.event, message.data || {});
  };
  socket.onclose = (event) => {
    if (socket !== ws) return;
    stopHeartbeat();
    if (event.code === 4401 || event.code === 4404) return shutDown(event.code);
    wsFails += 1;
    if (wsFails >= WS_MAX_FAILS) return fallback();
    setConn('连接断开，正在重连…', false);
    if (phase !== 'ended' && phase !== 'asked') setMain('正在重连…', false);
    setTimeout(connect, 800 * Math.min(wsFails + 1, 6));
  };
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (transport !== 'ws' || !ws || ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - lastPong > PONG_TIMEOUT_MS) {
      setConn('连接好像断了，正在重连…', false);
      try { ws.close(); } catch (error) { /* onclose 会接手 */ }
      return;
    }
    ws.send(JSON.stringify({type: 'ping'}));
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

/* 园内代理不支持 WebSocket 时，机器人照样能拍、能答、能结束 */
function fallback() {
  if (fatal || transport === 'http') return;
  transport = 'http';
  stopHeartbeat();
  if (ws) {
    try { ws.close(); } catch (error) { /* 忽略 */ }
    ws = null;
  }
  setConn('兼容模式', true);
  toast('实时连接不可用，已切换为兼容模式');
  pollSoon(0);
}

function pollSoon(delay) {
  clearTimeout(pollTimer);
  if (fatal) return;
  pollTimer = setTimeout(pollNow, delay || POLL_MS);
}

async function pollNow() {
  if (fatal) return;
  try {
    const response = await fetch(
      `/api/device/${R.sessionId}?token=${encodeURIComponent(R.token)}`,
      {cache: 'no-store'},
    );
    if (response.status === 403) return shutDown(4401);
    if (response.status === 404) return shutDown(4404);
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    const data = await response.json();
    setConn('兼容模式', true);
    applySnapshot(data.snapshot);
  } catch (error) {
    setConn('网络断开，正在重试…', false);
  } finally {
    pollSoon(POLL_MS);
  }
}

// ---------------- 状态复原 ----------------

/* 以服务端 snapshot 为准：重连、切兼容模式、刷新页面后都能回到正确状态 */
function applySnapshot(snapshot) {
  if (!snapshot) return;
  if (snapshot.last_thumb && snapshot.last_thumb.b64) lastThumb = snapshot.last_thumb;
  setState(snapshot.state_label || '准备中', true);
  const current = snapshot.current || null;
  const last = snapshot.last_turn || null;
  const turnId = current ? String(current.turn_id) : '';

  const answered = !!(current && last && String(last.id) === turnId && (last.reply || last.answer_text));
  if (answered) {
    const key = `reply:${current.page_no}`;
    if (renderedKey !== key) {
      renderedKey = key;
      renderReply({answer_text: last.answer_text, reply: last.reply});
    }
    clearAnalyzeWatch();
    nextPage = (snapshot.page_no || 0) + 1;
    phase = 'answered';
    setMain(`拍第 ${nextPage} 页`, true);
    return;
  }

  if (current && snapshot.state === 'LISTENING') {
    const key = `ask:${current.page_no}`;
    if (renderedKey !== key) {
      renderedKey = key;
      renderQuestion(current.question);
    }
    clearAnalyzeWatch();
    phase = 'asked';
    setMain('在听你说…', false);
    answerBar.style.display = 'flex';
    return;
  }

  if (snapshot.state === 'ANALYZING' || snapshot.state === 'CAPTURING') {
    phase = 'busy';
    setMain('识别中…', false);
    return;
  }

  if (snapshot.page_no) {
    clearAnalyzeWatch();
    nextPage = snapshot.page_no + 1;
    if (phase !== 'ended') {
      phase = 'answered';
      setMain(`拍第 ${nextPage} 页`, true);
    }
    return;
  }

  // 还没拍过页，或服务刚重启（page_no 归零）：也要把主按钮恢复出来，
  // 否则断线重连后会一直停在“正在重连…”无法继续。
  if (phase === 'ended') return;
  clearAnalyzeWatch();
  phase = 'idle';
  nextPage = Math.max(nextPage || 1, 1);
  setState(snapshot.state_label || '休息中', true);
  setMain(`拍第 ${nextPage} 页`, true);
}

function shutDown(code) {
  if (fatal) return;
  fatal = true;
  stopHeartbeat();
  clearTimeout(pollTimer);
  clearAnalyzeWatch();
  phase = 'ended';
  setState('本次共读结束', false);
  setConn('已结束', false);
  stage.innerHTML = `
    <div class="question" style="font-size:24px">${code === 4401 ? '请老师重新打开机器人端' : '这次共读已经结束了'}</div>
    <div class="hint">${code === 4401 ? '设备链接失效了，让老师重新生成一次。' : '老师已经在教师端看到本次记录和教学建议。'}</div>`;
  choicesBox.innerHTML = '';
  answerBar.style.display = 'none';
  cancelTalk();
  guardOff(true);
  setMain('本次共读结束', false);
}

// ---------------- 发送：WS 或 HTTP ----------------

function submit(payload) {
  if (phase === 'ended') return false;
  if (transport === 'http') {
    httpSubmit(payload);
    return true;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    toast('网络还没连上，等两秒再试一次');
    return false;
  }
  ws.send(JSON.stringify(payload));
  return true;
}

async function httpSubmit(payload) {
  const base = `/api/device/${R.sessionId}`;
  try {
    if (payload.type === 'answer') {
      await postJson(`${base}/answer`, {text: payload.text || '', token: R.token});
    } else if (payload.type === 'control') {
      await postJson(`${base}/control`, {action: payload.action || '', token: R.token});
    }
    pollSoon(300);
  } catch (error) {
    toast(error.message || '这一步没成功，再试一次');
    if (phase === 'busy') {
      phase = 'idle';
      setMain(`重拍第 ${nextPage} 页`, true);
    }
  }
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '这一步没成功');
  return data;
}

function handleServerError(message) {
  if (message.fatal) return shutDown(4404);
  toast(message.human_hint || '出了点问题');
  if (phase === 'busy') {
    clearAnalyzeWatch();
    phase = 'idle';
    setState('准备好了', false);
    setMain(`重拍第 ${nextPage} 页`, true);
  }
}

// ---------------- 事件（WS 实时通道） ----------------

function handleEvent(name, data) {
  if (name === 'state') {
    if (data.state_label) setState(data.state_label, true);
    if (data.state === 'ANALYZING' || data.state === 'CAPTURING') {
      phase = 'busy';
      setMain('识别中…', false);
      startAnalyzeWatch();
    }
    if (data.state === 'QUESTIONING') setMain('准备提问…', false);
    if (data.state === 'RESPONDING') setMain('我在想…', false);
    return;
  }
  if (name === 'page') {
    renderPicture(data);
    const read = data.read || {};
    showStory(read.text || '');
    showRest('');
    exitFree();
    playAudio((read.audio || {}).url, '点一下【再听一遍】听我讲这一页');
    phase = 'busy';
    setMain('快好了…', false);
    startAnalyzeWatch();
    return;
  }
  if (name === 'ask') {
    clearAnalyzeWatch();
    renderedKey = `ask:${data.page_no}`;
    phase = 'asked';
    setState('正在提问', true);
    renderQuestion(data.question);
    playAudio((data.audio || {}).url);
    answerBar.style.display = 'flex';
    setMain('在听你说…', false);
    return;
  }
  if (name === 'reply') {
    clearAnalyzeWatch();
    renderedKey = `reply:${data.page_no}`;
    phase = 'answered';
    setState('休息中', false);
    renderReply(data);
    playAudio((data.audio || {}).url);
    nextPage = (data.page_no || nextPage - 1) + 1;
    setMain(`拍第 ${nextPage} 页`, true);
    return;
  }
  if (name === 'free') {
    clearAnalyzeWatch();
    renderFree(data);
    playAudio((data.audio || {}).url, '点一下【再听一遍】就能听到回答');
    if (phase === 'busy' || phase === 'idle') phase = 'answered';
    setState(phase === 'asked' ? '在听你说' : '休息中', phase === 'asked');
    nextPage = Math.max(nextPage, 1);
    setMain(`拍第 ${nextPage} 页`, true);
    exitFree();
    return;
  }
  if (name === 'eye_rest') {
    clearAnalyzeWatch();
    showRest(data.human_hint || '让眼睛歇一歇，看看窗外远处。');
    stopAudio();
    playAudio((data.audio || {}).url);
    phase = phase === 'asked' ? 'asked' : 'answered';
    setState('休息一下', false);
    setMain(`拍第 ${nextPage} 页`, true);
    toast(data.human_hint || '让眼睛歇一歇');
    return;
  }
  if (name === 'quality') {
    clearAnalyzeWatch();
    phase = 'idle';
    setState('准备中', false);
    showStory('');
    showRest('');
    exitFree();
    stage.innerHTML = `<div class="placeholder">${escapeHtml(data.human_hint || '再拍一次好吗？')}</div>`;
    setMain(`重拍第 ${nextPage} 页`, true);
    toast(data.human_hint || '再拍一次好吗？');
    return;
  }
  if (name === 'timeout') {
    if (data.stage === 'icon_choice') {
      setState('在听你说', true);
      choicesBox.innerHTML = (data.choices || []).map((item) =>
        `<button class="choice" data-answer="${escapeHtml(item)}">${escapeHtml(item)}</button>`).join('');
      toast(data.human_hint || '没关系，我们一起看看');
    } else {
      toast(data.human_hint || '老师可以过来帮一下');
    }
    return;
  }
  if (name === 'skipped') {
    clearAnalyzeWatch();
    showStory('');
    showRest('');
    stopAudio();
    exitFree();
    phase = 'answered';
    nextPage = (data.page_no || nextPage - 1) + 1;
    setMain(`拍第 ${nextPage} 页`, true);
    toast(data.human_hint || '这一页先跳过');
    return;
  }
  if (name === 'teacher_talk') {
    phase = 'idle';
    showStory('');
    showRest('');
    stopAudio();
    exitFree();
    setState('老师来讲', false);
    setMain('我安静听', false);
    toast(data.human_hint || '老师来提问啦');
    return;
  }
  if (name === 'ended') {
    fatal = true;
    stopHeartbeat();
    clearTimeout(pollTimer);
    clearAnalyzeWatch();
    showStory('');
    showRest('');
    stopAudio();
    phase = 'ended';
    setState('共读结束', false);
    stage.innerHTML = `
      <div class="question" style="font-size:24px">今天读完了 ${data.pages || 0} 页</div>
      <div class="hint">老师已经在教师端看到本次记录和教学建议。</div>`;
    choicesBox.innerHTML = '';
    answerBar.style.display = 'none';
    cancelTalk();
    guardOff(true);
    setMain('本次共读结束', false);
    return;
  }
  if (name === 'error') {
    handleServerError(data);
  }
}

// ---------------- 识别看门狗 ----------------

function startAnalyzeWatch() {
  clearAnalyzeWatch();
  analyzeTimer = setTimeout(() => {
    if (phase !== 'busy') return;
    phase = 'idle';
    setState('准备好了', false);
    stage.innerHTML = '<div class="placeholder">这一页看得有点久，网络可能不太稳，再拍一次好吗？</div>';
    setMain(`重拍第 ${nextPage} 页`, true);
    toast('识别时间有点长，再拍一次好吗？');
  }, ANALYZE_TIMEOUT_MS);
}

function clearAnalyzeWatch() {
  if (analyzeTimer) clearTimeout(analyzeTimer);
  analyzeTimer = null;
}

// ---------------- 拍照与回答 ----------------

mainBtn.addEventListener('click', () => {
  if (fatal || phase === 'ended' || phase === 'asked') return;
  pickPage(nextPage);
});

/* 四种图片来源：机器人摄像头、本地图片、屏幕截图、剪贴板。
 * 没有 ImageSource（浏览器太旧或脚本未加载）时退回系统拍照控件。 */
async function pickPage(pageNo) {
  if (!window.ImageSource) {
    camera.click();
    return;
  }
  const shot = await ImageSource.pick({
    title: `拍第 ${pageNo} 页`,
    hint: '摄像头拍、网络摄像头、选本地图片、截屏或粘贴都可以',
  });
  if (!shot) return;
  sendImage(shot.dataUrl, pageNo);
}

function sendImage(dataUrl, pageNo) {
  const base64 = String(dataUrl || '').split(',')[1] || '';
  if (!base64) { toast('这张图没读出来，再试一张'); return; }
  stage.innerHTML = `<img class="pic" alt="这一页" src="${dataUrl}">`;
  phase = 'busy';
  setState('识别中', true);
  setMain('识别中…', false);
  startAnalyzeWatch();
  if (transport === 'http') {
    httpCapture(dataUrl, pageNo);
    return;
  }
  if (!submit({type: 'capture', page_no: pageNo, image: base64})) {
    clearAnalyzeWatch();
    phase = 'idle';
    setMain(`拍第 ${pageNo} 页`, true);
  }
}

async function httpCapture(dataUrl, pageNo) {
  try {
    const blob = await (await fetch(dataUrl)).blob();
    const form = new FormData();
    form.append('page_no', String(pageNo));
    form.append('token', R.token);
    form.append('file', blob, 'page.jpg');
    const response = await fetch(`/api/device/${R.sessionId}/page`, {method: 'POST', body: form});
    if (response.status === 403) return shutDown(4401);
    if (response.status === 404) return shutDown(4404);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || '这一页没传成功');
    }
    pollSoon(300);
  } catch (error) {
    clearAnalyzeWatch();
    phase = 'idle';
    setState('准备好了', false);
    setMain(`重拍第 ${pageNo} 页`, true);
    toast(error.message || '这一页没传成功，再试一次');
  }
}

camera.addEventListener('change', () => {
  const file = camera.files && camera.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => sendImage(reader.result, nextPage);
  reader.readAsDataURL(file);
  camera.value = '';
});

// ---------------- 语音回答：按住说话 ----------------
// 有键盘的平板可以打字，机器人上通常只有一块屏；"说一句"得是默认回答方式。
// 录完统一转 16k 单声道 WAV 再上传，真实 ASR 不用关心浏览器录的是什么格式。

let talk = null; // {recorder, startedAt} 录音中

function talkSupported() {
  return !!(window.Device && Device.profile.mic && Device.profile.recorder);
}

function setTalkLabel(text) {
  if (micBtn) micBtn.textContent = text;
}

async function startTalk(event) {
  if (event && event.cancelable) event.preventDefault();
  if (fatal || talk) return;
  if (talkMode === 'free') {
    if (phase === 'busy' || phase === 'ended') return;
  } else if (phase !== 'asked') {
    toast('等机器人提问后再回答，或者点【🙋 问一句】');
    return;
  }
  if (!talkSupported()) {
    toast('这台设备没有可用的麦克风，用文字或点图片回答');
    return;
  }
  try {
    const recorder = await Device.record({maxSeconds: 10, onTick: updateTalk});
    talk = {recorder: recorder, startedAt: Date.now()};
  } catch (error) {
    toast('麦克风没打开，检查浏览器权限');
    return;
  }
  if (micBtn) micBtn.classList.add('recording');
  setTalkLabel(talkMode === 'free' ? '松开提问 · 0s' : '松开发送 · 0s');
  setState('在听你说…', true);
  if (navigator.vibrate) navigator.vibrate(20);
}

function updateTalk(seconds) {
  setTalkLabel(`松开发送 · ${Math.min(seconds, 10).toFixed(0)}s`);
}

async function stopTalk(event) {
  if (event && event.cancelable) event.preventDefault();
  if (!talk) return;
  const job = talk;
  const seconds = (Date.now() - job.startedAt) / 1000;
  talk = null;
  if (micBtn) micBtn.classList.remove('recording');
  setTalkLabel(talkMode === 'free' ? '🎤 按住问' : '🎤 按住说话');
  job.recorder.stop();
  if (seconds < 0.4) {
    job.recorder.result().catch(() => {});
    toast('按住说话，说完再松手');
    return;
  }
  try {
    const out = await job.recorder.result();
    await sendAudio(out.blob);
  } catch (error) {
    toast('这段没录上，再说一次');
    setState('准备好了', false);
    setMain('在听你说…', false);
  }
}

function cancelTalk() {
  if (!talk) return;
  const job = talk;
  talk = null;
  if (micBtn) micBtn.classList.remove('recording');
  setTalkLabel('🎤 按住说话');
  try { job.recorder.stop(); } catch (error) { /* 已经在停 */ }
  job.recorder.result().catch(() => {});
}

async function sendAudio(blob) {
  const free = talkMode === 'free';
  setState('我在想…', true);
  setMain(free ? '我在听你问…' : '我在听…', false);
  answerBar.style.display = 'none';
  if (transport === 'http') {
    const form = new FormData();
    form.append('token', R.token);
    form.append('file', blob, 'answer.wav');
    const url = free
      ? `/api/device/${R.sessionId}/free-audio`
      : `/api/device/${R.sessionId}/listen`;
    try {
      const response = await fetch(url, {method: 'POST', body: form});
      if (response.status === 403) return shutDown(4401);
      if (response.status === 404) return shutDown(4404);
      if (!response.ok) throw new Error('没传成功');
      if (free) exitFree();
      pollSoon(300);
    } catch (error) {
      toast(free ? '这个问题没传成功，再问一次' : '这句没传成功，再说一次');
      answerBar.style.display = 'flex';
      setMain('在听你说…', false);
    }
    return;
  }
  const base64 = await blobToBase64(blob);
  if (!base64 || !submit({type: free ? 'free' : 'audio', audio: base64})) {
    toast(free ? '这段没录上，再问一次' : '这段没录上，再说一次');
    answerBar.style.display = 'flex';
    setMain('在听你说…', false);
  }
}

function blobToBase64(blob) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
    reader.onerror = () => resolve('');
    reader.readAsDataURL(blob);
  });
}

if (micBtn) {
  if (window.PointerEvent) {
    micBtn.addEventListener('pointerdown', startTalk);
    document.addEventListener('pointerup', stopTalk);
    document.addEventListener('pointercancel', stopTalk);
  } else {
    // 老 WebView 没有 PointerEvent：改成点一下开始、再点一下结束
    micBtn.addEventListener('click', () => { talk ? stopTalk() : startTalk(); });
  }
  micBtn.addEventListener('keydown', (event) => {
    if (event.key === ' ' && !event.repeat) { event.preventDefault(); startTalk(); }
  });
  micBtn.addEventListener('keyup', (event) => {
    if (event.key === ' ') { event.preventDefault(); stopTalk(); }
  });
}

if (freeBtn) freeBtn.addEventListener('click', enterFree);

// ---------------- 护眼：坐得太近就提醒 ----------------
// 只有浏览器支持 FaceDetector 才开；人脸比例只在本机内存里算，不拍、不存、不上传。
function guardOff(silent) {
  if (!guard) return;
  clearInterval(guard.timer);
  guard.stream.getTracks().forEach((track) => track.stop());
  if (guardVideo) guardVideo.srcObject = null;
  guard = null;
  if (guardBtn) guardBtn.textContent = '👀 护眼';
  if (!silent) toast('护眼检测已关');
}

async function guardOn() {
  if (!('FaceDetector' in window)) {
    toast('这个浏览器没有 FaceDetector，测不了距离；换 Chrome 桌面版试试');
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast('这台设备没有可用的前置摄像头');
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: 'user'}, audio: false,
    });
  } catch (error) {
    toast('没拿到前置摄像头权限，护眼提醒没开');
    return;
  }
  if (guardVideo) {
    guardVideo.srcObject = stream;
    guardVideo.play().catch(() => {});
  }
  guard = {
    stream,
    detector: new window.FaceDetector({fastMode: true, maxDetectedFaces: 1}),
    timer: setInterval(checkDistance, 3000),
    hits: 0,
    lastAt: 0,
  };
  if (guardBtn) guardBtn.textContent = '👀 护眼里';
  toast('护眼开着：只在本机算人脸大小，不拍、不存、不上传');
}

async function checkDistance() {
  if (!guard || !guardVideo || !guardVideo.videoWidth) return;
  let faces = [];
  try {
    faces = await guard.detector.detect(guardVideo);
  } catch (error) {
    guardOff(true);
    return;
  }
  if (!faces.length) { guard.hits = 0; return; }
  const height = (faces[0].boundingBox || {}).height || 0;
  const ratio = height / (guardVideo.videoHeight || 1);
  if (ratio <= 0.55) { guard.hits = 0; return; }  // 一臂远时人脸占比不会超过一半
  guard.hits += 1;
  if (guard.hits < 2) return;  // 连续两帧都太近才提醒，低头凑近看一眼不算
  const stamp = Date.now();
  if (stamp - guard.lastAt < 60000) return;  // 一分钟内只提醒一次
  guard.lastAt = stamp;
  guard.hits = 0;
  toast('坐远一点，眼睛离屏幕一臂远哦');
  try {
    const reply = await fetch(`/api/device/${R.sessionId}/remind`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: R.token, kind: 'distance'}),
    });
    if (reply.status === 403) return shutDown(4401);
    if (!reply.ok) return;
    const data = await reply.json();
    playAudio(((data || {}).audio || {}).url);
  } catch (error) { /* 服务端没合成出来：字已经提示过了 */ }
}

if (guardBtn) {
  guardBtn.addEventListener('click', () => {
    if (guard) guardOff(false);
    else guardOn();
  });
}

if (fsBtn) {
  fsBtn.addEventListener('click', () => {
    if (!window.Device) return;
    Device.fullscreen().then((on) => {
      toast(on ? '全屏了，再点右上角可以退出' : '这个设备不支持全屏');
    });
  });
}

choicesBox.addEventListener('click', (event) => {
  const button = event.target.closest('.choice');
  if (!button) return;
  submit({type: 'answer', text: button.dataset.answer || ''});
  choicesBox.innerHTML = '';
});

document.getElementById('answer-send').addEventListener('click', submitAnswer);
answerInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') submitAnswer();
});

function submitAnswer() {
  const text = answerInput.value.trim();
  if (!text) { toast('先说一句吧'); return; }
  answerInput.value = '';
  if (talkMode === 'free') {
    submit({type: 'free', text});
    setState('我在想…', true);
    exitFree();
    return;
  }
  submit({type: 'answer', text});
}

document.querySelectorAll('[data-control]').forEach((button) => {
  button.addEventListener('click', () => {
    const action = button.dataset.control;
    if (action === 'retake') {
      pickPage(nextPage);
      return;
    }
    submit({type: 'control', action});
  });
});

// ---------------- 网络状态 ----------------

window.addEventListener('offline', () => {
  setConn('网络已断开', false);
  if (phase !== 'ended') setMain('网络断开…', false);
});

window.addEventListener('online', () => {
  if (fatal) return;
  if (transport === 'http') {
    pollSoon(0);
  } else {
    wsFails = 0;
    connect();
  }
});

/* 平板息屏再亮起时，浏览器可能已把连接掐了而 onclose 没及时到 */
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible' || fatal) return;
  if (transport === 'http') {
    pollSoon(0);
    return;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    wsFails = 0;
    connect();
  }
});

// ---------------- 设备适配 ----------------
// 屏幕大小/方向由 device.js 写到 <html data-screen>，CSS 据此换布局；
// 能力不足时在这里一次性降级，不在业务代码里到处判断。

if (window.Device) {
  Device.keepAwake(); // 机器人放支架上，息屏就废了；不支持常亮就静默
  if (Device.profile.es2020 === false) {
    const note = document.getElementById('old-browser');
    if (note) note.hidden = false;
  }
  const tag = document.getElementById('device-tag');
  if (tag) tag.textContent = `本机 ${Device.deviceId.slice(-6)} · 屏幕 ${Device.screenClass()}`;
  if (!talkSupported() && micBtn) micBtn.hidden = true;
}

connect();