/* 设备适配层：把"这台机器人能做什么"变成可查询的能力清单。
 *
 * 教室里的设备差别很大：7 寸小屏/10 寸平板/横屏一体机、Chrome/Android WebView/Safari、
 * 有键盘的没键盘的、外接 USB 摄像头的没有摄像头的。页面只问能力，不猜型号。
 *
 * 挂到 window.Device：
 *   Device.profile      能力与屏幕快照（见下）
 *   Device.deviceId     本机持久 ID（localStorage，排障时对照日志）
 *   Device.fitMaxSide() 按性能选图片最长边（1600 / 1280 / 1024）
 *   Device.keepAwake()  屏幕常亮（不支持则静默失败）
 *   Device.fullscreen() 全屏开关
 *   Device.record()     录音 → 16k 单声道 WAV（失败自动回退原始格式）
 *   Device.onChange(fn) 屏幕尺寸/方向变化时回调
 */
(function () {
  'use strict';

  const ua = navigator.userAgent || '';
  const mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua);

  function detectEs2020() {
    try {
      // 可选链 + Promise.allSettled 是 Chromium 80 / Safari 14 的分水岭
      // eslint-disable-next-line no-new-func
      new Function('const o={a:1};return o?.a;');
      return typeof Promise.allSettled === 'function';
    } catch (error) {
      return false;
    }
  }

  function detectWebview() {
    if (/; wv\)/.test(ua)) return true; // Android WebView
    if (/MicroMessenger/i.test(ua)) return true; // 微信内置
    if (/iPhone|iPad|iPod/.test(ua) && !/Safari\//.test(ua)) return true;
    return false;
  }

  const media = navigator.mediaDevices || null;
  const profile = {
    es2020: detectEs2020(),
    secure: window.isSecureContext !== false,
    camera: !!(media && media.getUserMedia),
    mic: !!(media && media.getUserMedia),
    screen: !!(media && media.getDisplayMedia),
    enumerate: !!(media && media.enumerateDevices),
    recorder: typeof window.MediaRecorder !== 'undefined',
    audioContext: !!(window.AudioContext || window.webkitAudioContext),
    wakelock: 'wakeLock' in navigator,
    websocket: typeof window.WebSocket !== 'undefined',
    vibrate: typeof navigator.vibrate === 'function',
    cores: navigator.hardwareConcurrency || 0,
    memory: navigator.deviceMemory || 0,
    dpr: window.devicePixelRatio || 1,
    width: window.innerWidth,
    height: window.innerHeight,
    landscape: window.innerWidth > window.innerHeight,
    mobile: mobile,
    ios: /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1),
    android: /Android/i.test(ua),
    webview: detectWebview(),
  };

  function screenClass() {
    const w = window.innerWidth;
    if (w <= 480) return 'compact';   // 7 寸及以下竖屏
    if (w <= 900) return 'regular';   // 10 寸平板
    return 'wide';                    // 一体机/横屏笔记本
  }

  function applyAttributes() {
    const root = document.documentElement;
    profile.width = window.innerWidth;
    profile.height = window.innerHeight;
    profile.landscape = window.innerWidth > window.innerHeight;
    root.setAttribute('data-screen', screenClass());
    root.setAttribute('data-orientation', profile.landscape ? 'landscape' : 'portrait');
    root.setAttribute('data-mobile', profile.mobile ? '1' : '0');
    root.setAttribute('data-secure', profile.secure ? '1' : '0');
    root.setAttribute('data-lowmem', (profile.memory && profile.memory <= 2) || (profile.cores && profile.cores <= 4) ? '1' : '0');
    if (profile.es2020) root.classList.remove('old-browser');
    else root.classList.add('old-browser');
  }

  // ---- 本机 ID：换设备/多台机器人时，日志里能对上"是哪一台" ----
  const ID_KEY = 'reading_ai.device_id';

  function readDeviceId() {
    try {
      let value = window.localStorage.getItem(ID_KEY);
      if (!value) {
        const rand = Math.random().toString(36).slice(2, 8);
        value = 'dev-' + Date.now().toString(36) + '-' + rand;
        window.localStorage.setItem(ID_KEY, value);
      }
      return value;
    } catch (error) {
      return 'dev-anonymous'; // 隐私模式/禁用 storage 时不阻塞使用
    }
  }

  // ---- 图片尺寸：低配设备缩得更狠，避免解码拖垮拍照体验 ----
  function fitMaxSide() {
    if (profile.memory && profile.memory <= 1) return 1024;
    if ((profile.memory && profile.memory <= 2) || (profile.cores && profile.cores <= 4)) return 1280;
    return 1600;
  }

  // ---- 屏幕常亮：机器人放在支架上，息屏就废了；不支持就静默 ----
  let wakeSentinel = null;
  let wakeWanted = false;

  async function requestWake() {
    if (!profile.wakelock || document.visibilityState !== 'visible') return false;
    try {
      wakeSentinel = await navigator.wakeLock.request('screen');
      wakeSentinel.addEventListener('release', function () {
        wakeSentinel = null;
      });
      return true;
    } catch (error) {
      return false; // 电量低或系统策略禁止时浏览器会拒绝，不影响主流程
    }
  }

  function keepAwake() {
    wakeWanted = true;
    requestWake();
    document.addEventListener('visibilitychange', function () {
      if (wakeWanted && document.visibilityState === 'visible' && !wakeSentinel) requestWake();
    });
    return { get active() { return !!wakeSentinel; } };
  }

  function releaseAwake() {
    wakeWanted = false;
    if (wakeSentinel) {
      wakeSentinel.release().catch(function () {});
      wakeSentinel = null;
    }
  }

  // ---- 全屏：小屏上把浏览器地址栏藏掉，多出 40~60px ----
  function fullscreen() {
    const root = document.documentElement;
    const active = document.fullscreenElement || document.webkitFullscreenElement;
    if (active) {
      (document.exitFullscreen || document.webkitExitFullscreen || function () {}).call(document);
      return Promise.resolve(false);
    }
    const request = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!request) return Promise.resolve(false);
    return Promise.resolve(request.call(root))
      .then(function () { return true; })
      .catch(function () { return false; });
  }

  // ---- 录音：MediaRecorder 收集 → 解码重采样成 16k 单声道 WAV ----
  // 统一成 WAV 是因为服务端把音频原样交给 ASR，WAV 是所有 ASR 都能吃的最大公约数。
  function pickMime() {
    if (typeof window.MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
    for (let i = 0; i < candidates.length; i += 1) {
      if (MediaRecorder.isTypeSupported(candidates[i])) return candidates[i];
    }
    return '';
  }

  function encodeWav(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const write = function (offset, text) {
      for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
    };
    write(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    write(8, 'WAVE');
    write(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // 单声道
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (let i = 0; i < samples.length; i += 1) {
      const sample = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      offset += 2;
    }
    return new Blob([view], { type: 'audio/wav' });
  }

  async function toWav(blob, rate) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx || !window.OfflineAudioContext || !blob.arrayBuffer) return blob;
    const rateTarget = rate || 16000;
    try {
      const buffer = await blob.arrayBuffer();
      const ctx = new Ctx();
      // 新签名返回 Promise，旧签走回调；两种都兜住
      const decoded = await new Promise(function (resolve, reject) {
        const maybe = ctx.decodeAudioData(buffer, resolve, reject);
        if (maybe && typeof maybe.then === 'function') maybe.then(resolve, reject);
      });
      ctx.close();
      const frames = Math.max(1, Math.ceil(decoded.duration * rateTarget));
      const offline = new OfflineAudioContext(1, frames, rateTarget);
      const source = offline.createBufferSource();
      source.buffer = decoded;
      source.connect(offline.destination);
      source.start();
      const rendered = await offline.startRendering();
      return encodeWav(rendered.getChannelData(0), rateTarget);
    } catch (error) {
      return blob; // 解码失败就发原格式；stub ASR 不看内容，真实 ASR 视部署而定
    }
  }

  async function record(options) {
    const opts = Object.assign({ maxSeconds: 10, rate: 16000, onTick: null }, options || {});
    if (!profile.mic || !profile.recorder) throw new Error('这个设备不支持录音');
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    const mime = pickMime();
    const recorder = mime ? new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: 32000 }) : new MediaRecorder(stream);
    const chunks = [];
    let startedAt = Date.now();
    let timer = null;
    recorder.ondataavailable = function (event) {
      if (event.data && event.data.size) chunks.push(event.data);
    };
    const stopped = new Promise(function (resolve) {
      recorder.onstop = function () { resolve(); };
    });
    recorder.start(250);
    timer = setInterval(function () {
      const seconds = (Date.now() - startedAt) / 1000;
      if (opts.onTick) opts.onTick(seconds);
      if (seconds >= opts.maxSeconds) recorder.stop();
    }, 200);
    return {
      stop: function () {
        if (recorder.state !== 'inactive') recorder.stop();
      },
      result: async function () {
        await stopped;
        clearInterval(timer);
        stream.getTracks().forEach(function (track) { track.stop(); });
        const raw = new Blob(chunks, { type: mime || 'audio/webm' });
        const wav = await toWav(raw, opts.rate);
        return { blob: wav, seconds: (Date.now() - startedAt) / 1000, mimetype: wav.type || mime };
      },
    };
  }

  // ---- 尺寸变化通知（横竖屏切换、小屏键盘弹出）----
  const listeners = [];
  let resizeTimer = null;

  function onChange(fn) {
    if (typeof fn === 'function') listeners.push(fn);
  }

  window.addEventListener('resize', function () {
    applyAttributes();
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      listeners.forEach(function (fn) {
        try { fn(profile); } catch (error) { /* 单个回调出错不影响其他 */ }
      });
    }, 200);
  });
  window.addEventListener('orientationchange', applyAttributes);

  applyAttributes();

  window.Device = {
    profile: profile,
    deviceId: readDeviceId(),
    screenClass: screenClass,
    fitMaxSide: fitMaxSide,
    keepAwake: keepAwake,
    releaseAwake: releaseAwake,
    fullscreen: fullscreen,
    record: record,
    onChange: onChange,
    supports: function (name) { return !!profile[name]; },
  };
})();
