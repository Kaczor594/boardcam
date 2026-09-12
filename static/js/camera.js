/* Camera page: holds the video stream, captures a burst on every clock press,
   and uploads the frames. Protocol: docs/PROTOCOL.md §3.3, §4. */

import { GameSocket, api, keepAwake, modal, setDot, pulseDot, toast } from './ws.js';
import { renderQr } from './qr.js';

const BURST_OFFSETS_MS = [0, 400, 900];   // PROTOCOL.md §4
const PRESTART_INTERVAL_MS = 2000;
const LONG_EDGE = 1280;
const JPEG_QUALITY = 0.85;
const UPLOAD_RETRY_MS = 800;

const el = {
  setup: document.getElementById('setup'),
  startBtn: document.getElementById('start-btn'),
  restartBtn: document.getElementById('restart-btn'),
  video: document.getElementById('preview'),
  overlay: document.getElementById('overlay'),
  stage: document.getElementById('stage'),
  roomCode: document.getElementById('room-code'),
  qrSlot: document.getElementById('qr-slot'),
  joinUrl: document.getElementById('join-url'),
  clockDot: document.getElementById('clock-dot'),
  streamDot: document.getElementById('stream-dot'),
  frameDot: document.getElementById('frame-dot'),
  calDot: document.getElementById('cal-dot'),
  frameLabel: document.getElementById('frame-label'),
  uploadLabel: document.getElementById('upload-label'),
  errorLine: document.getElementById('error-line'),
  gameIdLabel: document.getElementById('game-id'),
  cornerPanel: document.getElementById('corner-panel'),
  cornerStill: document.getElementById('corner-still'),
  cornerSvg: document.getElementById('corner-svg'),
  cornerUse: document.getElementById('corner-use'),
  cornerCancel: document.getElementById('corner-cancel'),
};

let game = null;          // {game_id, room, ...}
let socket = null;
let stream = null;
let prestartTimer = null;
let burstTimers = [];
let started = false;      // true once the first capture command arrives
let uploaded = 0;
let dropped = 0;

const canvas = document.createElement('canvas');
const ctx = canvas.getContext('2d', { alpha: false });

/* ---------------- game setup ---------------- */

async function ensureGame() {
  const existing = new URLSearchParams(location.search).get('game');
  if (existing) {
    try {
      // A reload after a crash must rejoin, not orphan the game.
      const detail = await api(`/api/games/${existing}`);
      return { game_id: detail.game_id, room: detail.room };
    } catch {
      toast('That game is gone. Starting a new one.');
    }
  }
  const created = await api('/games', { method: 'POST', body: {} });
  const url = new URL(location.href);
  url.searchParams.set('game', created.game_id);
  history.replaceState(null, '', url);
  return created;
}

function showPairing() {
  el.roomCode.textContent = game.room;
  el.gameIdLabel.textContent = game.game_id;

  const joinUrl = `${location.origin}/clock?room=${game.room}`;
  el.joinUrl.textContent = joinUrl;
  el.joinUrl.href = joinUrl;

  const svg = renderQr(joinUrl, { className: 'qr' });
  el.qrSlot.replaceChildren();
  if (svg) {
    el.qrSlot.appendChild(svg);
  } else {
    // Never show a broken QR: the room code is the real pairing mechanism.
    el.qrSlot.hidden = true;
  }
}

/* ---------------- camera ---------------- */

async function startCamera() {
  el.errorLine.hidden = true;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1920 } },
      audio: false,
    });
  } catch (err) {
    showCameraError(err.name || String(err));
    return;
  }

  el.video.srcObject = stream;
  el.video.setAttribute('playsinline', '');
  try {
    await el.video.play();
  } catch (err) {
    showCameraError(`play failed: ${err.name || err}`);
    return;
  }

  el.setup.hidden = true;
  el.stage.hidden = false;
  el.restartBtn.hidden = true;
  setDot(el.streamDot, 'ok');
  sendCameraStatus();

  stream.getVideoTracks()[0].addEventListener('ended', () => {
    setDot(el.streamDot, 'error');
    el.restartBtn.hidden = false;
    sendCameraStatus('track ended');
  });

  startPrestartLoop();
}

function showCameraError(name) {
  setDot(el.streamDot, 'error');
  el.errorLine.textContent = `Camera unavailable: ${name}`;
  el.errorLine.hidden = false;
  el.restartBtn.hidden = false;
  el.setup.hidden = false;
  sendCameraStatus(name);
}

function sendCameraStatus(error = null) {
  const track = stream && stream.getVideoTracks()[0];
  const settings = track ? track.getSettings() : {};
  socket?.send({
    type: 'camera.status',
    streaming: Boolean(track && track.readyState === 'live'),
    w: settings.width || 0,
    h: settings.height || 0,
    error,
  });
}

/* ---------------- capture ---------------- */

/** Draw the current video frame to the shared canvas, long edge LONG_EDGE. */
function grabBlob() {
  const vw = el.video.videoWidth;
  const vh = el.video.videoHeight;
  if (!vw || !vh) return null;
  const scale = Math.min(1, LONG_EDGE / Math.max(vw, vh));
  const w = Math.round(vw * scale);
  const h = Math.round(vh * scale);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  ctx.drawImage(el.video, 0, 0, w, h);
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', JPEG_QUALITY));
}

async function upload(seq, k, blob, retry = true) {
  const form = new FormData();
  form.append('file', blob, `${seq}_${k}.jpg`);
  try {
    await api(`/games/${game.game_id}/frames/${seq}/${k}`, { method: 'POST', body: form });
    uploaded += 1;
    el.frameLabel.textContent = `frame #${seq}`;
    pulseDot(el.frameDot);
    setDot(el.frameDot, 'ok');
    refreshCounters();
  } catch (err) {
    if (retry) {
      // One retry, then give up: a stalled upload must never hold up the game.
      setTimeout(() => upload(seq, k, blob, false), UPLOAD_RETRY_MS);
      return;
    }
    dropped += 1;
    el.errorLine.textContent = `Upload failed (frame ${seq}.${k}): ${err.message}`;
    el.errorLine.hidden = false;
    setDot(el.frameDot, 'warn');
    refreshCounters();
  }
}

function refreshCounters() {
  el.uploadLabel.textContent = dropped
    ? `${uploaded} up / ${dropped} lost`
    : `${uploaded} up`;
}

async function captureOne(seq, k) {
  const blob = await grabBlob();
  if (blob) {
    upload(seq, k, blob);
    return;
  }
  // The stream had no frame to give. Count it rather than losing it silently,
  // so the operator can see the camera is not actually recording.
  dropped += 1;
  setDot(el.frameDot, 'warn');
  el.errorLine.textContent = `No frame from the camera (${seq}.${k}). Is the stream live?`;
  el.errorLine.hidden = false;
  refreshCounters();
}

function cancelBurst() {
  burstTimers.forEach(clearTimeout);
  burstTimers = [];
}

/** Grab three frames at +0/+400/+900 ms. A new capture cancels what is left. */
function startBurst(seq) {
  cancelBurst();
  BURST_OFFSETS_MS.forEach((delay, k) => {
    if (delay === 0) {
      captureOne(seq, k);
    } else {
      burstTimers.push(setTimeout(() => captureOne(seq, k), delay));
    }
  });
}

/* ---------------- pre-start frame 0 ---------------- */

function startPrestartLoop() {
  stopPrestartLoop();
  captureOne(0, 0);
  prestartTimer = setInterval(() => {
    if (started) { stopPrestartLoop(); return; }
    captureOne(0, 0);
  }, PRESTART_INTERVAL_MS);
}

function stopPrestartLoop() {
  if (prestartTimer) { clearInterval(prestartTimer); prestartTimer = null; }
}

/* ---------------- overlay ---------------- */

/**
 * Draw the detected board quadrilateral. Corners are four [x, y] pairs in
 * 0..1 of the frame. Phase 1 never calls this; Phase 3 does, once calibration
 * reports corners.
 */
export function drawCalibration(corners) {
  el.overlay.replaceChildren();
  if (!corners || corners.length !== 4) return;
  const NS = 'http://www.w3.org/2000/svg';
  const poly = document.createElementNS(NS, 'polygon');
  poly.setAttribute('points', corners.map(([x, y]) => `${x * 100},${y * 100}`).join(' '));
  poly.setAttribute('fill', 'none');
  poly.setAttribute('stroke', 'var(--moss-30)');
  poly.setAttribute('stroke-width', '0.6');
  poly.setAttribute('vector-effect', 'non-scaling-stroke');
  el.overlay.appendChild(poly);
}

/** Keep the overlay exactly over the letterboxed video, not the whole element. */
function syncOverlay() {
  const vw = el.video.videoWidth;
  const vh = el.video.videoHeight;
  if (!vw || !vh) return;
  const box = el.video.getBoundingClientRect();
  const scale = Math.min(box.width / vw, box.height / vh);
  const w = vw * scale;
  const h = vh * scale;
  el.overlay.style.width = `${w}px`;
  el.overlay.style.height = `${h}px`;
  el.overlay.style.left = `${(box.width - w) / 2}px`;
  el.overlay.style.top = `${(box.height - h) / 2}px`;
}

window.addEventListener('resize', syncOverlay);
window.addEventListener('orientationchange', () => setTimeout(syncOverlay, 300));
el.video.addEventListener('loadedmetadata', syncOverlay);

/* ---------------- manual corner fallback ---------------- */

// Board order, not screen order: the engine reads these as a1, h1, h8, a8 and
// that single ordering is what tells it which way round the board is.
const CORNER_NAMES = ['a1', 'h1', 'h8', 'a8'];
const cornerState = [
  { x: 0.2, y: 0.3 }, { x: 0.8, y: 0.3 },
  { x: 0.85, y: 0.8 }, { x: 0.15, y: 0.8 },
];

function openCornerPanel() {
  grabBlob().then((blob) => {
    if (blob) el.cornerStill.src = URL.createObjectURL(blob);
  });
  el.cornerPanel.hidden = false;
  drawHandles();
}

function drawHandles() {
  const NS = 'http://www.w3.org/2000/svg';
  el.cornerSvg.replaceChildren();

  const poly = document.createElementNS(NS, 'polygon');
  poly.setAttribute('points', cornerState.map((p) => `${p.x * 100},${p.y * 100}`).join(' '));
  poly.setAttribute('fill', 'rgba(143,164,116,0.15)');
  poly.setAttribute('stroke', '#8FA474');
  poly.setAttribute('stroke-width', '0.5');
  el.cornerSvg.appendChild(poly);

  cornerState.forEach((p, i) => {
    const g = document.createElementNS(NS, 'circle');
    g.setAttribute('cx', String(p.x * 100));
    g.setAttribute('cy', String(p.y * 100));
    g.setAttribute('r', '3.2');          // ~44px at typical panel sizes
    g.setAttribute('fill', '#FDFCF8');
    g.setAttribute('stroke', '#3E5A32');
    g.setAttribute('stroke-width', '0.8');
    g.dataset.index = String(i);
    g.style.cursor = 'grab';

    const label = document.createElementNS(NS, 'text');
    label.setAttribute('x', String(p.x * 100));
    label.setAttribute('y', String(p.y * 100 + 1.2));
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('font-size', '3');
    label.setAttribute('fill', '#3E5A32');
    label.style.pointerEvents = 'none';
    label.textContent = CORNER_NAMES[i];
    g.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      g.setPointerCapture(ev.pointerId);
      const move = (m) => {
        const box = el.cornerSvg.getBoundingClientRect();
        cornerState[i] = {
          x: Math.min(1, Math.max(0, (m.clientX - box.left) / box.width)),
          y: Math.min(1, Math.max(0, (m.clientY - box.top) / box.height)),
        };
        drawHandles();
      };
      const up = () => {
        g.removeEventListener('pointermove', move);
        g.removeEventListener('pointerup', up);
      };
      g.addEventListener('pointermove', move);
      g.addEventListener('pointerup', up);
    });
    el.cornerSvg.appendChild(g);
    el.cornerSvg.appendChild(label);
  });
}

el.cornerCancel.addEventListener('click', () => { el.cornerPanel.hidden = true; });
el.cornerUse.addEventListener('click', async () => {
  if (!game) {
    toast('No game to calibrate yet.');
    return;
  }
  // The panel works in normalised coordinates, and the engine reads the stored
  // JPEG — which is the video scaled to LONG_EDGE, not the video itself.
  const vw = el.video.videoWidth || 1;
  const vh = el.video.videoHeight || 1;
  const scale = Math.min(1, LONG_EDGE / Math.max(vw, vh));
  const w = Math.round(vw * scale);
  const h = Math.round(vh * scale);
  const corners = cornerState.map((p) => [
    Number((p.x * w).toFixed(1)), Number((p.y * h).toFixed(1))]);
  try {
    await api(`/api/games/${game.game_id}/corners`, { method: 'POST', body: { corners } });
    el.cornerPanel.hidden = true;
    toast('Corners saved.');
  } catch (err) {
    toast(`Could not save corners: ${err.message}`);
  }
});

/* ---------------- socket ---------------- */

function onMessage(msg) {
  switch (msg.type) {
    case 'hello':
      setDot(el.clockDot, msg.peer_connected ? 'ok' : null);
      break;
    case 'peer':
      if (msg.role === 'clock') setDot(el.clockDot, msg.connected ? 'ok' : null);
      break;
    case 'capture':
      started = true;
      stopPrestartLoop();
      startBurst(msg.seq);
      break;
    case 'calibration':
      setDot(el.calDot, msg.ok ? 'ok' : (msg.warning ? 'warn' : 'error'));
      if (msg.ok && msg.corners) drawCalibration(msg.corners);
      if (!msg.ok) openCornerPanel();
      break;
    case 'analysis.ready': {
      const flagged = Array.isArray(msg.flagged) ? msg.flagged.length : (msg.flagged || 0);
      toast(flagged ? `Game recorded — ${flagged} ply to check.` : 'Game recorded.');
      break;
    }
    default:
      break;   // later phases add message types; ignoring them is intentional
  }
}

/* ---------------- boot ---------------- */

el.startBtn.addEventListener('click', startCamera);
el.restartBtn.addEventListener('click', startCamera);

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible' || !stream) return;
  // iOS pauses the element when the tab goes away; a dead track needs a fresh
  // user gesture, which is what the restart button is for.
  const track = stream.getVideoTracks()[0];
  if (!track || track.readyState !== 'live') {
    setDot(el.streamDot, 'error');
    el.restartBtn.hidden = false;
    return;
  }
  if (el.video.paused) el.video.play().catch(() => {});
  syncOverlay();
});

async function boot() {
  keepAwake();
  refreshCounters();
  try {
    game = await ensureGame();
  } catch (err) {
    el.errorLine.textContent = `Could not create a game: ${err.message}`;
    el.errorLine.hidden = false;
    return;
  }
  showPairing();

  socket = new GameSocket(game.room, 'camera');
  socket.onmessage = onMessage;
  socket.onopen = () => sendCameraStatus();
  socket.onstate = (state) => {
    if (state !== 'open') setDot(el.clockDot, null);
  };
  socket.onclose = (code) => {
    if (code === 4000) {
      modal({
        title: 'Camera opened elsewhere',
        body: 'Another device took over as the camera for this game. This page is no longer recording.',
        choices: [{ label: 'OK', value: true }],
      });
    }
  };
  socket.connect();
}

boot();
