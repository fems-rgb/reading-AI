/* 图片来源多通道：摄像头 / 本地文件 / 屏幕截图 / 剪贴板粘贴。
 *
 * 用法：
 *   const shot = await ImageSource.pick({title: '拍下这一页'});
 *   if (shot) { shot.blob; shot.dataUrl; shot.width; shot.height; shot.source; }
 *
 * 说明：
 * - 摄像头与屏幕截图需要安全上下文（https 或 localhost），否则浏览器直接禁用；
 *   此时按钮会置灰并写明原因，文件与粘贴始终可用。
 * - 统一缩到最长边 1600px 再上传，避免平板原图几百 KB 的 base64 卡住实时链路。
 */
(function () {
  const MAX_SIDE = 1600;
  
  /* 低配机器人解码大图慢：device.js 会根据内存/核心数降到 1280 或 1024 */
  function maxSide() {
    if (window.Device && typeof Device.fitMaxSide === 'function') return Device.fitMaxSide();
    return MAX_SIDE;
  }
  const QUALITY = 0.85;
  const SOURCES = {
    camera: {label: '摄像头', icon: '📷', hint: '把书平放，对着这一页'},
    net: {label: '网络摄像头', icon: '📡', hint: 'ESP32 / 手机 IP 摄像头 / 电脑 USB 桥接'},
    file: {label: '本地图片', icon: '🖼️', hint: '从相册或文件夹里选一张'},
    screen: {label: '屏幕截图', icon: '🖥️', hint: '选一个窗口或整块屏幕'},
    paste: {label: '粘贴', icon: '📋', hint: '按 Ctrl+V 粘贴剪贴板里的图片'},
  };

  const mediaSupported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const secure = window.isSecureContext !== false;
  const displaySupported = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);

  /* 翻页自动拍：市面绘本机器人的卖点是「翻到哪页读哪页」，
   * 这里用极轻的帧差检测（40x40 灰度采样，400ms 一次）实现：
   * 画面抖动（翻页中）→ 稳定 2 拍 → 且跟基准帧确实不同 → 自动抓拍。
   * 阈值故意保守，误拍也只多一次「再拍一次」，比漏拍体验好。 */
  const FLIP_TICK_MS = 400;
  const FLIP_SAMPLE = 40;
  const FLIP_MOVE_DIFF = 5;
  const FLIP_STABLE_DIFF = 4;
  const FLIP_CHANGE_DIFF = 8;
  const FLIP_STABLE_TICKS = 2;
  const FLIP_WARMUP_MS = 1500;
  const FLIP_AUTO_CONFIRM_MS = 1500;
  const AUTO_KEY = 'reading_ai.auto_flip';

  /* 网络摄像头（ESP32-CAM / 手机 IP Webcam / 电脑 USB 桥接）：
   * 浏览器直接读像素会被跨域拦住（摄像头不会发 CORS 头），所以：
   * - 预览：直接 <img src>，不读像素，不受跨域限制；
   * - 抓帧：交给服务端代理（同源），顺便把 SSRF 白名单收在服务端。 */
  const NET_KEY = 'reading_ai.net_cams';
  const NET_TICK_MS = 1500;   // 网络摄像头自动翻页的采样间隔（ESP32 扛不住高频）
  const NET_PREVIEW_MS = 800; // 单帧地址的预览刷新间隔

  let box = null;
  let dom = null;
  let stream = null;
  let session = null; // {resolve, source, lastBlob, lastDataUrl, mode}
  let pasteBound = false;
  let flip = null;      // 翻页检测状态
  let autoTimer = null; // 自动拍后“再确认”的倒计时
  let netTimer = null;  // 网络摄像头预览刷新

  function readNetCams() {
    try {
      const list = JSON.parse(localStorage.getItem(NET_KEY) || '[]');
      return Array.isArray(list) ? list.filter((item) => item && item.url) : [];
    } catch (error) { return []; }
  }

  function writeNetCams(list) {
    try { localStorage.setItem(NET_KEY, JSON.stringify(list.slice(0, 8))); } catch (error) { /* 无痕模式忽略 */ }
  }

  function rememberNetCam(name, url) {
    const list = readNetCams();
    const index = list.findIndex((item) => item.url === url);
    const item = {name: name || `网络摄像头 ${list.length + 1}`, url};
    if (index >= 0) list[index] = item; else list.unshift(item);
    writeNetCams(list);
  }

  function esc(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function renderNetOptions(currentUrl) {
    if (!dom || !dom.netList) return;
    const list = readNetCams();
    if (!list.length) {
      dom.netList.innerHTML = '<option value="">（还没保存过摄像头）</option>';
      dom.netList.disabled = true;
      return;
    }
    dom.netList.disabled = false;
    dom.netList.innerHTML = list.map((item) => (
      `<option value="${esc(item.url)}"${item.url === currentUrl ? ' selected' : ''}>${esc(item.name)}</option>`
    )).join('');
  }

  function proxyUrl(url) {
    const robot = window.ROBOT || {};
    return '/api/camera/snapshot?' + new URLSearchParams({
      session_id: robot.sessionId || '',
      token: robot.token || '',
      url,
      t: String(Date.now()),
    }).toString();
  }

  async function netGrabBlob() {
    const response = await fetch(proxyUrl(session.netUrl), {cache: 'no-store'});
    if (!response.ok) {
      let detail = `抓帧失败（${response.status}）`;
      try { const body = await response.json(); if (body && body.detail) detail = body.detail; } catch (error) { /* 非 JSON */ }
      throw new Error(detail);
    }
    return response.blob();
  }

  function isMjpeg(url) {
    return /(stream|mjpg|mjpeg|\.mjpg|video)/i.test(url);
  }

  function hasCredentials(url) {
    // http://账号:密码@192.168.1.64/… —— 浏览器不许 <img> 直接加载带凭证的子资源
    return /^https?:\/\/[^/@\s]+@/i.test(url);
  }

  function stopNetPreview() {
    if (netTimer) clearInterval(netTimer);
    netTimer = null;
    const img = dom && dom.netImg;
    if (img) {
      img.hidden = true;
      if (img.getAttribute('src')) img.removeAttribute('src'); // 断开 MJPEG 长连接
    }
  }

  function startNetPreview() {
    stopNetPreview();
    const img = dom.netImg;
    if (!img || !session || !session.netUrl) return;
    const url = session.netUrl;
    img.hidden = false;
    const credentials = hasCredentials(url);
    if (!credentials && isMjpeg(url)) {
      // MJPEG 是长连接：设一次 src 浏览器会自己持续接收，别轮询重设
      img.src = url;
      img.onerror = () => {
        if (!session || session.mode !== 'net-live') return;
        dom.hint.textContent = '这个流地址浏览器打不开：换成抓帧地址（如 /shot.jpg、/capture）再连一次。';
      };
      return;
    }
    const direct = () => { img.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now(); };
    const viaProxy = () => { img.src = proxyUrl(url); };
    if (credentials) {
      // 浏览器会拦掉 <img> 里带账号密码的地址，直接让服务端带着凭证去拉
      session.proxyPreview = true;
    }
    img.onerror = () => {
      if (!session || session.mode !== 'net-live' || session.proxyPreview) return;
      // 机器人跟摄像头不在同一网段时浏览器直连不到，但服务端能到 → 改走代理预览
      session.proxyPreview = true;
      dom.hint.textContent = '浏览器直连不到摄像头，已改由服务端转发预览';
      viaProxy();
    };
    let last = 0;
    const tick = () => {
      if (!session || session.mode !== 'net-live') { stopNetPreview(); return; }
      const gap = session.proxyPreview ? NET_TICK_MS : NET_PREVIEW_MS;
      const now = Date.now();
      if (now - last < gap) return;
      last = now;
      if (session.proxyPreview) viaProxy(); else direct();
    };
    tick();
    netTimer = setInterval(tick, 400);
  }

  function autoFlipEnabled() {
    try { return localStorage.getItem(AUTO_KEY) !== '0'; } catch (error) { return true; }
  }

  function sampleFrame() {
    const video = dom && dom.video;
    if (!video || !video.videoWidth) return null;
    const canvas = flip.canvas;
    const ctx = canvas.getContext('2d', {willReadFrequently: true});
    ctx.drawImage(video, 0, 0, FLIP_SAMPLE, FLIP_SAMPLE);
    return ctx.getImageData(0, 0, FLIP_SAMPLE, FLIP_SAMPLE).data;
  }

  function frameDiff(current, baseline) {
    if (!current || !baseline) return 999;
    let sum = 0;
    for (let index = 0; index < current.length; index += 4) {
      sum += Math.abs(current[index] - baseline[index]);
    }
    return sum / (current.length / 4);
  }

  function stopFlipWatch() {
    if (flip && flip.timer) clearInterval(flip.timer);
    flip = null;
  }

  function startFlipWatch() {
    stopFlipWatch();
    if (!autoFlipEnabled()) return;
    const netMode = !!(session && session.mode === 'net-live');
    flip = {
      canvas: document.createElement('canvas'),
      timer: 0,
      prev: null,
      base: null,
      moving: false,
      stable: 0,
      busy: false,
      readyAfter: Date.now() + (netMode ? FLIP_TICK_MS * 2 : FLIP_WARMUP_MS),
    };
    flip.canvas.width = FLIP_SAMPLE;
    flip.canvas.height = FLIP_SAMPLE;
    flip.timer = setInterval(netMode ? tickNetFlip : tickFlip, netMode ? NET_TICK_MS : FLIP_TICK_MS);
  }

  function judgeFrame(current) {
    if (!flip) return false;
    // 刚打开摄像头前 1.5s 画面还在自动曝光，不能当翻页
    if (Date.now() < flip.readyAfter) {
      flip.base = current; flip.prev = current;
      return false;
    }
    const delta = frameDiff(current, flip.prev || current);
    if (delta >= FLIP_MOVE_DIFF) {
      flip.moving = true;
      flip.stable = 0;
    } else if (flip.moving) {
      flip.stable += 1;
    }
    flip.prev = current;
    if (!flip.moving || flip.stable < FLIP_STABLE_TICKS) return false;
    const changed = frameDiff(current, flip.base) >= FLIP_CHANGE_DIFF;
    flip.moving = false;
    flip.stable = 0;
    flip.base = current;
    return changed;
  }

  function tickFlip() {
    if (!flip || !session || session.mode !== 'live') { stopFlipWatch(); return; }
    const current = sampleFrame();
    if (!current || !judgeFrame(current)) return;
    stopFlipWatch();
    captureFrame(true);
  }

  function blobToBitmap(blob) {
    if (window.createImageBitmap) return createImageBitmap(blob);
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const image = new Image();
      image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
      image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片解不开')); };
      image.src = url;
    });
  }

  async function sampleBlob(blob) {
    const bitmap = await blobToBitmap(blob);
    const ctx = flip.canvas.getContext('2d', {willReadFrequently: true});
    ctx.drawImage(bitmap, 0, 0, FLIP_SAMPLE, FLIP_SAMPLE);
    if (bitmap.close) bitmap.close();
    return ctx.getImageData(0, 0, FLIP_SAMPLE, FLIP_SAMPLE).data;
  }

  /* 网络摄像头自动翻页：ESP32 扛不住高频请求，1.5 秒拉一帧，拉下来的帧直接复用；
   * 网络抖动只跳过这一拍，不算翻页。 */
  async function tickNetFlip() {
    if (!flip || !session || session.mode !== 'net-live') { stopFlipWatch(); return; }
    if (flip.busy) return;
    flip.busy = true;
    let current = null;
    let blob = null;
    try {
      blob = await netGrabBlob();
      current = flip ? await sampleBlob(blob) : null;
    } catch (error) {
      current = null;
    }
    if (flip) flip.busy = false;
    if (!current || !flip || !session || session.mode !== 'net-live') return;
    if (!judgeFrame(current)) return;
    stopFlipWatch();
    useBlob(blob, 'net', true);
  }

  /* 摄像头在非安全上下文里拿不到 getUserMedia，但仍可以走系统相机/相册；
   * 屏幕共享没有任何兼容退路，只能用真断开。 */
  function cameraIsLive() {
    return mediaSupported && secure;
  }

  function unavailableReason(kind) {
    if (kind === 'screen') {
      if (!secure) return '当前地址不是 https 或 localhost，浏览器禁用了屏幕共享；请改用摄像头或文件。';
      if (!displaySupported) return '这个浏览器不支持屏幕共享，请改用摄像头或文件。';
    }
    return '';
  }

  function ensureDom() {
    if (dom) return dom;
    box = document.createElement('div');
    box.className = 'src-mask';
    box.innerHTML = `
      <div class="src-panel" role="dialog" aria-modal="true">
        <div class="src-head">
          <b id="src-title">选择图片来源</b>
          <button class="src-close" data-src="cancel" aria-label="关闭">×</button>
        </div>
        <p class="src-hint" id="src-hint"></p>
        <div class="src-choices" id="src-choices"></div>
        <select class="src-cams" id="src-cams" aria-label="选择摄像头" hidden></select>
        <div class="src-net" id="src-net" style="display:none">
          <div class="src-net-row">
            <select id="src-net-list" aria-label="已保存的网络摄像头"></select>
            <button class="btn ghost" data-src="net-forget">删除</button>
            <button class="btn ghost" data-src="net-back">返回</button>
          </div>
          <div class="src-net-row">
            <input id="src-net-name" placeholder="名字（如 教室 ESP32）">
            <input id="src-net-url" placeholder="http://192.168.1.50/capture">
            <button class="btn" data-src="net-connect">连接</button>
          </div>
          <p class="small muted">填能返回一张 JPEG 的地址：ESP32-CAM 用 <code>/capture</code>，
            手机 IP Webcam 用 <code>/shot.jpg</code>，电脑 USB 摄像头用 <code>_tools/camera_bridge.py</code> 启动后显示的地址。
            只给 MJPEG 流（<code>/stream</code>、<code>/video</code>）的也能用：预览直连，抓帧由服务端从流里抠一张。
            要登录的摄像头把账号密码写进地址：<code>http://账号:密码@192.168.1.64/capture</code>（Basic / Digest 都行）；
            只给 RTSP 的先用 <code>camera_bridge.py --url</code> 转成本机 http 地址。</p>
        </div>
        <div class="src-stage" id="src-stage" style="display:none">
          <video id="src-video" playsinline muted hidden></video>
          <img id="src-net-img" alt="网络摄像头" hidden>
          <img id="src-preview" alt="预览" hidden>
        </div>
        <div class="src-actions" id="src-actions" style="display:none">
          <button class="btn ghost" data-src="reset">重来</button>
          <button class="btn" data-src="confirm" id="src-confirm">用这张</button>
        </div>
        <label class="src-auto" id="src-auto-wrap">
          <input type="checkbox" id="src-auto"> 翻页自动拍（不用每页按按钮）
        </label>
        <input id="src-file" type="file" accept="image/*" hidden>
        <input id="src-camera" type="file" accept="image/*" capture="environment" hidden>
        <p class="src-note small muted" id="src-note"></p>
      </div>`;
    document.body.appendChild(box);
    dom = {
      title: box.querySelector('#src-title'),
      hint: box.querySelector('#src-hint'),
      choices: box.querySelector('#src-choices'),
      stage: box.querySelector('#src-stage'),
      video: box.querySelector('#src-video'),
      preview: box.querySelector('#src-preview'),
      actions: box.querySelector('#src-actions'),
      confirm: box.querySelector('#src-confirm'),
      file: box.querySelector('#src-file'),
      camera: box.querySelector('#src-camera'),
      note: box.querySelector('#src-note'),
      cams: box.querySelector('#src-cams'),
      auto: box.querySelector('#src-auto'),
      autoWrap: box.querySelector('#src-auto-wrap'),
      netBox: box.querySelector('#src-net'),
      netList: box.querySelector('#src-net-list'),
      netName: box.querySelector('#src-net-name'),
      netUrl: box.querySelector('#src-net-url'),
      netImg: box.querySelector('#src-net-img'),
    };
    box.addEventListener('click', onClick);
    dom.cams.addEventListener('change', () => {
      writeCameraId(dom.cams.value);
      if (session && session.mode === 'live') startCamera();
    });
    dom.netList.addEventListener('change', () => {
      const picked = readNetCams().find((item) => item.url === dom.netList.value);
      if (!picked) return;
      dom.netName.value = picked.name;
      dom.netUrl.value = picked.url;
    });
    [dom.netName, dom.netUrl].forEach((input) => {
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); connectNet(); }
      });
    });
    dom.auto.checked = autoFlipEnabled();
    dom.auto.addEventListener('change', () => {
      try { localStorage.setItem(AUTO_KEY, dom.auto.checked ? '1' : '0'); } catch (error) { /* 无痕模式忽略 */ }
      if (!dom.auto.checked) stopFlipWatch();
      else if (session && (session.mode === 'live' || session.mode === 'net-live')) startFlipWatch();
    });
    dom.file.addEventListener('change', () => handleFiles(dom.file.files));
    dom.camera.addEventListener('change', () => handleFiles(dom.camera.files));
    bindPaste();
    return dom;
  }

  function bindPaste() {
    if (pasteBound) return;
    pasteBound = true;
    document.addEventListener('paste', (event) => {
      if (!session) return;
      const items = (event.clipboardData && event.clipboardData.items) || [];
      for (const item of items) {
        if (item.type && item.type.startsWith('image/')) {
          event.preventDefault();
          const file = item.getAsFile();
          if (file) useBlob(file, 'paste');
          return;
        }
      }
    });
    // 机器人上可能接鼠标键盘；Esc 应当是关面板的通用习惯
    document.addEventListener('keydown', (event) => {
      if (session && event.key === 'Escape') {
        event.preventDefault();
        finish(null);
      }
    });
  }

  function renderChoices(allowed) {
    dom.choices.innerHTML = allowed.map((kind) => {
      const info = SOURCES[kind];
      const reason = unavailableReason(kind);
      const systemCamera = kind === 'camera' && !cameraIsLive();
      const label = systemCamera ? '系统相机' : info.label;
      return `<button class="src-choice" data-src="use" data-kind="${kind}" ${reason ? 'disabled' : ''}>
        <span class="src-icon">${info.icon}</span>
        <span class="src-label">${label}</span>
        ${reason ? '<span class="src-why">不可用</span>' : ''}
      </button>`;
    }).join('');
    const blocked = allowed
      .map((kind) => [kind, unavailableReason(kind)])
      .filter(([, reason]) => reason);
    if (blocked.length) {
      dom.note.textContent = blocked[0][1];
    } else if (!cameraIsLive()) {
      dom.note.textContent = '当前不是 https 或 localhost：摄像头会交给系统相机/相册，屏幕共享不可用。';
    } else {
      dom.note.textContent = '';
    }
  }

  function resetStage() {
    stopFlipWatch();
    clearTimeout(autoTimer);
    autoTimer = null;
    stopStream();
    dom.stage.style.display = 'none';
    dom.actions.style.display = 'none';
    dom.video.hidden = true;
    dom.preview.hidden = true;
    dom.video.srcObject = null;
    dom.preview.removeAttribute('src');
    dom.netImg.hidden = true;
    dom.choices.style.display = '';
    dom.cams.hidden = true;
    dom.netBox.style.display = 'none';
    dom.autoWrap.hidden = true; // 只有实时预览时才需要“翻页自动拍”
    if (session) {
      session.lastBlob = null;
      session.lastDataUrl = '';
    }
  }

  function stopStream() {
    stopFlipWatch();
    clearTimeout(autoTimer);
    autoTimer = null;
    stopNetPreview();
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
  }

  // 多摄像头（机器人外接 USB / 平板前后置）：记住选过的那一个
  const CAM_KEY = 'reading_ai.camera_id';

  function readCameraId() {
    try { return window.localStorage.getItem(CAM_KEY) || ''; } catch (error) { return ''; }
  }

  function writeCameraId(id) {
    try {
      if (id) window.localStorage.setItem(CAM_KEY, id);
      else window.localStorage.removeItem(CAM_KEY);
    } catch (error) { /* 隐私模式下不记也行 */ }
  }

  async function listCameras() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
    let cams = (await navigator.mediaDevices.enumerateDevices()).filter((item) => item.kind === 'videoinput');
    if (cams.length && !cams[0].label) {
      // 未授权时 label 为空，先请求一次权限才能看出哪台是哪台
      try {
        const probe = await navigator.mediaDevices.getUserMedia({video: true});
        probe.getTracks().forEach((track) => track.stop());
      } catch (error) { /* 用户拒绝就用无名设备列表 */ }
      cams = (await navigator.mediaDevices.enumerateDevices()).filter((item) => item.kind === 'videoinput');
    }
    return cams;
  }

  function renderCamOptions(cams, currentId) {
    if (!dom.cams) return;
    if (cams.length < 2) {
      dom.cams.hidden = true;
      return;
    }
    dom.cams.innerHTML = cams.map((cam, index) => {
      const name = cam.label || `摄像头 ${index + 1}`;
      const selected = cam.deviceId === currentId ? ' selected' : '';
      return `<option value="${cam.deviceId}"${selected}>${name}</option>`;
    }).join('');
    dom.cams.hidden = false;
  }

  async function startCamera() {
    if (!cameraIsLive()) {
      dom.camera.click(); // 非安全上下文：交给系统相机/相册
      return;
    }
    stopStream();
    const wanted = readCameraId();
    const constraints = {
      video: wanted
        ? {deviceId: {exact: wanted}, width: {ideal: 1920}, height: {ideal: 1440}}
        : {facingMode: {ideal: 'environment'}, width: {ideal: 1920}, height: {ideal: 1440}},
      audio: false,
    };
    dom.hint.textContent = '正在打开摄像头…';
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (error) {
      // 记住的那台拔掉了（外接 USB 常见）→ 清记忆再试默认后置
      if (wanted) writeCameraId('');
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: {facingMode: {ideal: 'environment'}, width: {ideal: 1920}, height: {ideal: 1440}},
          audio: false,
        });
      } catch (fallbackError) {
        dom.hint.textContent = '摄像头没打开：' + describe(fallbackError) + '。可以直接选本地图片。';
        dom.camera.click();
        return;
      }
    }
    const track = stream.getVideoTracks()[0];
    const currentId = (track && track.getSettings && track.getSettings().deviceId) || wanted;
    if (currentId) writeCameraId(currentId);
    enterLiveMode('camera');
    listCameras().then((cams) => renderCamOptions(cams, currentId)).catch(() => {});
  }

  async function startScreen() {
    if (unavailableReason('screen')) return;
    dom.hint.textContent = '正在请求屏幕共享…';
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({video: {frameRate: 10}, audio: false});
    } catch (error) {
      dom.hint.textContent = '屏幕共享没有开始：' + describe(error);
      return;
    }
    enterLiveMode('screen');
  }

  function enterLiveMode(kind) {
    dom.choices.style.display = 'none';
    dom.stage.style.display = '';
    dom.video.hidden = false;
    dom.preview.hidden = true;
    dom.video.srcObject = stream;
    dom.video.play().catch(() => {});
    dom.actions.style.display = '';
    dom.confirm.textContent = '拍下来';
    dom.hint.textContent = kind === 'screen' ? '把绘本页面放到屏幕上，对齐后点【拍下来】' : '把这一页放满画面，点【拍下来】';
    dom.autoWrap.hidden = false;
    if (session) session.mode = 'live';
    startFlipWatch();
    // 用户在浏览器工具栏上停掉共享/拔掉摄像头：track 会 ended，不能停在黑屏上
    stream.getVideoTracks().forEach((track) => {
      track.addEventListener('ended', () => {
        if (!session || session.mode !== 'live') return;
        stopStream();
        dom.stage.style.display = 'none';
        dom.video.hidden = true;
        dom.actions.style.display = 'none';
        dom.choices.style.display = '';
        session.mode = 'idle';
        dom.hint.textContent = kind === 'screen' ? '屏幕共享结束了，可以重新选一次。' : '摄像头被关掉了，可以重新选一次。';
      });
    });
  }

  function enterNetMode() {
    if (!session || !session.netUrl) return;
    dom.choices.style.display = 'none';
    dom.netBox.style.display = 'none';
    dom.stage.style.display = '';
    dom.video.hidden = true;
    dom.video.srcObject = null;
    dom.preview.hidden = true;
    dom.netImg.hidden = false;
    dom.actions.style.display = '';
    dom.confirm.textContent = '拍下来';
    session.mode = 'net-live';
    session.source = 'net';
    session.proxyPreview = false;
    dom.hint.textContent = '连上了：画面会自动刷新，点【拍下来】抓取当前一帧';
    dom.autoWrap.hidden = false;
    startNetPreview();
    startFlipWatch();
  }

  async function connectNet() {
    if (!session) return;
    const url = (dom.netUrl.value || '').trim();
    const name = (dom.netName.value || '').trim();
    if (!url) {
      dom.hint.textContent = '先把地址填上，比如 http://192.168.1.50/capture';
      return;
    }
    if (/^rtsps?:\/\//i.test(url)) {
      dom.hint.textContent = 'RTSP 流要先转换：电脑上运行 _tools/camera_bridge.py --url ' + url +
        '，把它显示的 http 地址填到这里。';
      return;
    }
    session.netUrl = url;
    dom.hint.textContent = '正在试连摄像头…';
    try {
      await netGrabBlob(); // 先抓一帧：地址不通、返回网页、太大数据都当场说清
    } catch (error) {
      dom.hint.textContent = '连不上：' + (error && error.message ? error.message : '地址不通');
      return;
    }
    rememberNetCam(name, url);
    renderNetOptions(url);
    enterNetMode();
  }

  function forgetNet() {
    const url = (dom.netList.value || (session && session.netUrl) || '').trim();
    if (!url) return;
    writeNetCams(readNetCams().filter((item) => item.url !== url));
    renderNetOptions('');
    if (session && session.netUrl === url) {
      stopStream();
      session.netUrl = '';
      session.mode = 'idle';
      dom.stage.style.display = 'none';
      dom.actions.style.display = 'none';
      dom.netImg.hidden = true;
      dom.netBox.style.display = '';
      dom.hint.textContent = '已删除这条记录，可以重新填地址连接。';
    }
  }

  function describe(error) {
    const name = (error && error.name) || '';
    if (name === 'NotAllowedError') return '你取消了授权';
    if (name === 'NotFoundError') return '没有找到设备';
    if (name === 'NotReadableError') return '设备被其他程序占用了';
    return (error && error.message) || '浏览器不允许';
  }

  async function captureFrame(auto) {
    if (session && session.mode === 'net-live') {
      // 网络摄像头的像素浏览器读不到（跨域会污染 canvas），抓帧一律走服务端代理
      dom.hint.textContent = auto ? '检测到翻页，正在抓帧…' : '正在抓帧…';
      try {
        const blob = await netGrabBlob();
        await useBlob(blob, 'net', !!auto);
      } catch (error) {
        dom.hint.textContent = (error && error.message ? error.message : '抓帧失败') + '；检查摄像头是否在线。';
        if (auto && session) startFlipWatch(); // 自动拍失败，继续盯着画面
      }
      return;
    }
    const video = dom.video;
    if (!video || !video.videoWidth) {
      dom.hint.textContent = '画面还没准备好，再等半秒点一次。';
      return;
    }
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, maxSide() / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => useBlob(blob, session && session.mode === 'live' ? session.source : 'camera', !!auto), 'image/jpeg', QUALITY);
  }

  async function useBlob(blob, source, auto) {
    if (!blob || !session) return;
    const shrunk = await shrink(blob);
    session.lastBlob = shrunk.blob;
    session.lastDataUrl = shrunk.dataUrl;
    session.lastWidth = shrunk.width;
    session.lastHeight = shrunk.height;
    stopStream();
    dom.stage.style.display = '';
    dom.video.hidden = true;
    dom.netImg.hidden = true;
    dom.preview.hidden = false;
    dom.preview.src = shrunk.dataUrl;
    dom.choices.style.display = 'none';
    dom.actions.style.display = '';
    dom.confirm.textContent = '用这张';
    dom.autoWrap.hidden = true; // 已经拍下了，自动翻页先收起来，重新连上再出现
    if (session) session.mode = 'still';
    if (!auto) {
      dom.hint.textContent = `已取得图片（${shrunk.width}×${shrunk.height}）`;
      return;
    }
    // 自动拍：给 1.5 秒反悔窗口，不动就自动用这张（孩子不用再按）
    dom.hint.textContent = `检测到翻页，已拍下（${shrunk.width}×${shrunk.height}）；${FLIP_AUTO_CONFIRM_MS / 1000} 秒后自动用这张，不对就点【重来】`;
    clearTimeout(autoTimer);
    autoTimer = setTimeout(() => {
      if (session && session.mode === 'still') {
        finish({
          blob: session.lastBlob,
          dataUrl: session.lastDataUrl,
          source: session.source,
          width: session.lastWidth,
          height: session.lastHeight,
        });
      }
    }, FLIP_AUTO_CONFIRM_MS);
  }

  function shrink(blob) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob);
      const image = new Image();
      image.onload = () => {
        const scale = Math.min(1, maxSide() / Math.max(image.naturalWidth, image.naturalHeight));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(image.naturalWidth * scale);
        canvas.height = Math.round(image.naturalHeight * scale);
        canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        const dataUrl = canvas.toDataURL('image/jpeg', QUALITY);
        canvas.toBlob((out) => {
          resolve({blob: out || blob, dataUrl, width: canvas.width, height: canvas.height});
        }, 'image/jpeg', QUALITY);
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        resolve({blob, dataUrl: url, width: 0, height: 0});
      };
      image.src = url;
    });
  }

  function handleFiles(files) {
    const file = files && files[0];
    if (file) useBlob(file, session ? session.source : 'file');
    if (dom.file) dom.file.value = '';
    if (dom.camera) dom.camera.value = '';
  }

  function onClick(event) {
    const action = event.target.closest('[data-src]');
    if (!action || action.disabled) return;
    const kind = action.dataset.kind;
    if (action.dataset.src === 'cancel') return finish(null);
    if (action.dataset.src === 'reset') return resetStage();
    if (action.dataset.src === 'use') {
      if (session) session.source = kind;
      dom.hint.textContent = SOURCES[kind].hint;
      if (kind === 'camera') return startCamera();
      if (kind === 'screen') return startScreen();
      if (kind === 'net') {
        dom.choices.style.display = 'none';
        dom.netBox.style.display = '';
        renderNetOptions(session ? session.netUrl : '');
        dom.hint.textContent = '填好摄像头地址后点【连接】，连上就能看到画面';
        return;
      }
      if (kind === 'file') return dom.file.click();
      if (kind === 'paste') {
        dom.hint.textContent = '按 Ctrl+V（平板长按输入框粘贴）把图片粘进来';
        return;
      }
    }
    if (action.dataset.src === 'net-back') {
      dom.netBox.style.display = 'none';
      dom.choices.style.display = '';
      dom.hint.textContent = '摄像头拍、网络摄像头、本地图片、截屏或粘贴，任选一种';
      return;
    }
    if (action.dataset.src === 'net-connect') return connectNet();
    if (action.dataset.src === 'net-forget') return forgetNet();
    if (action.dataset.src === 'confirm') {
      if (!session) return;
      if (session.mode === 'live' || session.mode === 'net-live') return captureFrame();
      if (!session.lastBlob) return;
      return finish({
        blob: session.lastBlob,
        dataUrl: session.lastDataUrl,
        source: session.source,
        width: session.lastWidth,
        height: session.lastHeight,
      });
    }
  }

  function finish(result) {
    const done = session;
    session = null;
    stopStream();
    if (box) box.classList.remove('open');
    if (result) {
      done.resolve({
        blob: result.blob,
        dataUrl: result.dataUrl,
        source: result.source,
        width: result.width || 0,
        height: result.height || 0,
      });
    } else {
      done.resolve(null);
    }
  }

  function pick(options) {
    const opts = Object.assign({title: '选择图片来源', sources: ['camera', 'net', 'file', 'screen', 'paste']}, options || {});
    ensureDom();
    resetStage();
    return new Promise((resolve) => {
      session = {resolve, source: 'file', mode: 'idle', lastBlob: null, lastDataUrl: '', lastWidth: 0, lastHeight: 0, netUrl: '', proxyPreview: false};
      dom.title.textContent = opts.title;
      dom.hint.textContent = opts.hint || '摄像头拍照、网络摄像头、本地图片、屏幕截图、粘贴，任选一种';
      renderChoices(opts.sources.filter((kind) => SOURCES[kind]));
      box.classList.add('open');
    });
  }

  window.ImageSource = {
    pick,
    available: () => ({media: mediaSupported && secure, screen: displaySupported && secure, secure}),
    reason: unavailableReason,
  };
})();