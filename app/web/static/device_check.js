/* 设备自检页：换一台机器人先跑一遍，确认摄像头/麦克风/喇叭/网络都正常。
 *
 * 这里刻意不依赖业务接口、不写数据库——只做本地能力验证，
 * 也不显示任何孩子数据，所以不需要登录。
 */
const $ = (id) => document.getElementById(id);

const CAPS = [
  ['safe', '安全上下文（https 或 localhost）', '摄像头、屏幕共享的前提'],
  ['camera', '网页内摄像头', '不支持时自动改用系统相机'],
  ['mic', '麦克风', '「按住说话」回答的前提'],
  ['recorder', '录音（MediaRecorder）', '没有就只能打字回答'],
  ['screen', '屏幕共享', '电子绘本/远程支援用'],
  ['wakelock', '屏幕常亮（Wake Lock）', '放支架上不熄屏'],
  ['websocket', 'WebSocket', '不支持会切 HTTP 兼容模式'],
  ['es2020', '浏览器能力（ES2020）', '太旧会显示升级提示'],
];

let camStream = null;
let micCtx = null;
let micStream = null;
let micRaf = null;

function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(window.__checkToast);
  window.__checkToast = setTimeout(() => el.classList.remove('show'), 2400);
}

function renderCaps() {
  const profile = window.Device ? Device.profile : {};
  const body = $('caps').querySelector('tbody');
  body.innerHTML = CAPS.map(([key, label, why]) => {
    const ok = key === 'safe' ? profile.secure : !!profile[key];
    return `<tr><td>${label}</td><td style="width:64px;text-align:center">${ok ? '✅' : '❌'}</td>
      <td class="small muted">${why}</td></tr>`;
  }).join('');
  if (window.Device) {
    $('id-line').textContent = `本机 ${Device.deviceId} · 屏幕 ${Device.screenClass()} · ` +
      `${profile.width}×${profile.height} @${profile.dpr}x · 核心 ${profile.cores || '?'} · 内存 ${profile.memory || '?'}GB`;
  }
  const critical = ['secure', 'camera', 'mic', 'recorder', 'websocket', 'es2020'];
  const missing = critical.filter((key) => !(key === 'safe' ? profile.secure : profile[key]));
  const text = missing.length ? `有 ${missing.length} 项关键能力缺失` : '关键能力齐全，可以上架';
  $('summary-text').textContent = text;
  $('summary').classList.toggle('live', missing.length === 0);
}

// ---------------- 摄像头 ----------------

async function listCams() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
  let cams = (await navigator.mediaDevices.enumerateDevices()).filter((item) => item.kind === 'videoinput');
  if (cams.length && !cams[0].label) {
    // 没授权时 label 是空的，打开一次才有名字
    try {
      const probe = await navigator.mediaDevices.getUserMedia({video: true});
      probe.getTracks().forEach((track) => track.stop());
    } catch (error) { /* 下面按无名设备显示 */ }
    cams = (await navigator.mediaDevices.enumerateDevices()).filter((item) => item.kind === 'videoinput');
  }
  return cams;
}

function stopCam() {
  if (camStream) {
    camStream.getTracks().forEach((track) => track.stop());
    camStream = null;
  }
  $('cam').srcObject = null;
}

async function openCam(deviceId) {
  stopCam();
  const video = deviceId
    ? {deviceId: {exact: deviceId}, width: {ideal: 1920}, height: {ideal: 1440}}
    : {facingMode: {ideal: 'environment'}, width: {ideal: 1920}, height: {ideal: 1440}};
  try {
    camStream = await navigator.mediaDevices.getUserMedia({video: video, audio: false});
  } catch (error) {
    const name = (error && error.name) || '';
    const map = {
      NotAllowedError: '你取消了授权',
      NotFoundError: '没有找到摄像头',
      NotReadableError: '摄像头被其他程序占用了',
      OverconstrainedError: '这台摄像头不支持所需分辨率',
    };
    $('cam-note').textContent = '打不开：' + (map[name] || name || '浏览器不允许');
    return;
  }
  const track = camStream.getVideoTracks()[0];
  const settings = (track && track.getSettings) ? track.getSettings() : {};
  $('cam').srcObject = camStream;
  // 只设 srcObject 不少内核（尤其 iOS/部分 WebView）不会自动起播，预览就是黑板
  $('cam').play().catch(() => {});
  $('cam-note').textContent = `正在预览：${(track && track.label) || '默认摄像头'} · ` +
    `${settings.width || '?'}×${settings.height || '?'}`;
  // 能力表只看得到 API 存在，这里补一条“真的出画面了没有”
  setTimeout(() => {
    if (!camStream) return;
    const live = $('cam').videoWidth > 0 && $('cam').videoHeight > 0;
    $('cam-note').textContent = live
      ? `正在预览：${(track && track.label) || '默认摄像头'} · ${settings.width || '?'}×${settings.height || '?'} · 有画面`
      : `摄像头开了但没画面：${(track && track.label) || '默认摄像头'} · 换一个浏览器或重插一下 USB 摄像头`;
  }, 1200);
  const cams = await listCams();
  if (cams.length > 1) {
    $('cam-list').innerHTML = cams.map((cam, index) =>
      `<option value="${cam.deviceId}"${cam.deviceId === settings.deviceId ? ' selected' : ''}>` +
      `${cam.label || `摄像头 ${index + 1}`}</option>`).join('');
    $('cam-list').hidden = false;
    $('cam-list').style.display = '';
  } else {
    $('cam-list').hidden = true;
    $('cam-list').style.display = 'none';
  }
}

$('cam-start').addEventListener('click', () => openCam());
$('cam-list').addEventListener('change', () => openCam($('cam-list').value));

// ---------------- 网络摄像头（ESP32-CAM / 手机 IP Webcam / 电脑桥接） ----------------
// 自检页不登录，所以这里只做「这台设备能不能直连看到画面」；
// 真正的抓帧由服务端代抓（防跨域），在取图面板里连一次就算全链路验证了。
const NET_KEY = 'reading_ai.net_cams';

function readNetCams() {
  try {
    const list = JSON.parse(localStorage.getItem(NET_KEY) || '[]');
    return Array.isArray(list) ? list.filter((item) => item && item.url) : [];
  } catch (error) { return []; }
}

function rememberNetCam(url) {
  const list = readNetCams();
  list.unshift({name: '自检添加', url});
  const unique = [];
  for (const item of list) {
    if (!unique.some((kept) => kept.url === item.url)) unique.push(item);
  }
  try { localStorage.setItem(NET_KEY, JSON.stringify(unique.slice(0, 8))); } catch (error) { /* 无痕模式忽略 */ }
}

let netProbe = 0; // 防连点：每次试连一个序号，旧的回调直接作废

function testNetCam() {
  const url = ($('netcam-url').value || '').trim();
  if (/^rtsps?:\/\//i.test(url)) {
    $('netcam-note').textContent = 'RTSP 流浏览器取不了图：先用 _tools/camera_bridge.py --url 转成本机 http 地址再试。';
    return;
  }
  if (/^https?:\/\/[^/@\s]+@/i.test(url)) {
    $('netcam-note').textContent = '地址里带了账号密码：浏览器不让直连，去取图面板选「网络摄像头」，那边会由服务端带着凭证转发。';
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    $('netcam-note').textContent = '地址要写成 http://192.168.1.50/capture 这样，只支持 http（局域网摄像头一般没有证书）。';
    return;
  }
  netProbe += 1;
  const ticket = netProbe;
  const img = $('netcam-img');
  img.hidden = false;
  $('netcam-note').textContent = '正在连…最多等 3 秒。';
  const started = performance.now();
  let settled = false;
  const settle = () => {
    if (settled || ticket !== netProbe) return;
    settled = true;
    const live = img.naturalWidth > 0 && img.naturalHeight > 0;
    const cost = Math.round(performance.now() - started);
    if (live) {
      rememberNetCam(url);
      $('netcam-note').textContent = `有画面：${img.naturalWidth}×${img.naturalHeight}，${cost}ms · 已记着，取图面板选「网络摄像头」直接用`;
    } else {
      $('netcam-note').textContent = `没画面（${cost}ms）：核对 IP/端口、摄像头是否通电、和本机是否同一 WiFi。` +
        'ESP32 用 /capture；手机 IP Webcam 用 /shot.jpg；若浏览器看不到但服务端能到，取图面板会自动改走服务端预览。';
    }
  };
  img.onload = settle;
  img.onerror = settle;
  img.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now();
  // MJPEG 流在有些内核上不触发 onload，3 秒后直接看有没有像素
  setTimeout(settle, 3000);
}

$('netcam-run').addEventListener('click', () => testNetCam());
$('netcam-url').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); testNetCam(); }
});
(function prefillNetCam() {
  const list = readNetCams();
  if (list.length) $('netcam-url').value = list[0].url;
})();

// ---------------- 麦克风 ----------------

async function startMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    $('mic-note').textContent = '这个浏览器不支持麦克风采集。';
    return;
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({audio: true});
  } catch (error) {
    $('mic-note').textContent = '打不开麦克风：' + ((error && error.name) || '浏览器不允许');
    return;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  micCtx = new Ctx();
  const source = micCtx.createMediaStreamSource(micStream);
  const analyser = micCtx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const data = new Uint8Array(analyser.fftSize);
  $('mic-start').hidden = true;
  $('mic-stop').hidden = false;
  $('mic-note').textContent = '正在检测，说一句话试试。';
  const loop = () => {
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (let i = 0; i < data.length; i += 1) {
      peak = Math.max(peak, Math.abs(data[i] - 128) / 128);
    }
    $('mic-fill').style.width = Math.min(100, Math.round(peak * 140)) + '%';
    micRaf = requestAnimationFrame(loop);
  };
  loop();
}

function stopMic() {
  if (micRaf) cancelAnimationFrame(micRaf);
  micRaf = null;
  if (micCtx) { micCtx.close().catch(() => {}); micCtx = null; }
  if (micStream) {
    micStream.getTracks().forEach((track) => track.stop());
    micStream = null;
  }
  $('mic-fill').style.width = '0%';
  $('mic-start').hidden = false;
  $('mic-stop').hidden = true;
  $('mic-note').textContent = '已停止。';
}

$('mic-start').addEventListener('click', () => startMic());
$('mic-stop').addEventListener('click', () => stopMic());

// ---------------- 喇叭 ----------------

$('spk-play').addEventListener('click', () => {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) { $('spk-note').textContent = '这个浏览器不支持播放音效。'; return; }
  const ctx = new Ctx();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.value = 660;
  gain.gain.setValueAtTime(0.001, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.6);
  osc.connect(gain).connect(ctx.destination);
  osc.start();
  osc.stop(ctx.currentTime + 0.65);
  osc.onended = () => ctx.close();
  $('spk-note').textContent = '播过了：听到「叮」就正常，没听到先检查系统音量与静音键。';
});

// ---------------- 网络 ----------------

async function testNet() {
  $('net-note').textContent = '正在测…';
  const samples = [];
  for (let i = 0; i < 5; i += 1) {
    const started = performance.now();
    try {
      await fetch('/healthz?ts=' + Date.now(), {cache: 'no-store'});
      samples.push(performance.now() - started);
    } catch (error) {
      $('net-note').textContent = '连不上服务：检查网线/WiFi，或服务是否启动。';
      return;
    }
  }
  const min = Math.min.apply(null, samples);
  const avg = samples.reduce((sum, value) => sum + value, 0) / samples.length;
  const verdict = avg < 50 ? '很好' : avg < 200 ? '一般（心跳会兜住）' : '偏慢（可能会切兼容模式）';
  $('net-note').textContent = `往返 最小 ${min.toFixed(0)}ms / 平均 ${avg.toFixed(0)}ms · ${verdict}`;
}

$('net-run').addEventListener('click', () => testNet());

// ---------------- 复制设备信息 ----------------

function report() {
  const profile = window.Device ? Device.profile : {};
  return JSON.stringify({
    device_id: window.Device ? Device.deviceId : 'unknown',
    screen: window.Device ? Device.screenClass() : '',
    ua: navigator.userAgent,
    profile: profile,
    at: new Date().toISOString(),
  }, null, 2);
}

$('copy').addEventListener('click', () => {
  const text = report();
  const fallback = () => {
    // 局域网 http 下没有 clipboard API，用临时 textarea 兜底
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
    document.body.removeChild(area);
    toast(ok ? '已复制，可粘给技术老师' : '这个浏览器不让复制，请手动截图');
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast('已复制，可粘给技术老师'), fallback);
  } else {
    fallback();
  }
});

renderCaps();
