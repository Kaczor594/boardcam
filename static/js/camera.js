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
  lockBtn: document.getElementById('lock-btn'),
  lockState: document.getElementById('lock-state'),
};

let game = null;          // {game_id, room, ...}
let socket = null;
let stream = null;
let prestartTimer = null;
let burstTimers = [];
let started = false;      // true once the first capture command arrives
let startLocked = false;  // true once the players say the board is set
let manualCorners = false;// true once a player has placed the corners by hand
let uploaded = 0;
let dropped = 0;

const canvas = document.createElement('canvas');
const ctx = canvas.getContext('2d', { alpha: false });

/* ---------------- game setup ---------------- */

async function ensureGame() {
  const existing = new URLSearchParams(location.search).get('game');
  if (existing) {
    try {
      // A reload mid-game must rejoin, not orphan the game. A *finished* game
      // must never be rejoined: its frames are numbered, its clock has stopped,
      // and a second session would be recorded on top of the first one — two
      // games in one directory, which is not recoverable from the phone.
      const detail = await api(`/api/games/${existing}`);
      if (detail.status !== 'finished') {
        return { game_id: detail.game_id, room: detail.room };
      }
      toast('That game is finished. Starting a new one.');
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
    if (started || startLocked) { stopPrestartLoop(); return; }
    captureOne(0, 0);
  }, PRESTART_INTERVAL_MS);
}

/**
 * Freeze the start position.
 *
 * Until this is pressed the start frame keeps being replaced every couple of
 * seconds, and the last replacement lands *after* White has moved — the clock's
 * first press is White's move, not the start of the game. The engine would then
 * be tracking against a board that already has a move on it, and every ply
 * after it reads wrong. One tap when the pieces are placed is what stops that.
 */
async function lockStartFrame() {
  if (!game || startLocked) return;
  el.lockBtn.disabled = true;
  await captureOne(0, 0);
  startLocked = true;
  stopPrestartLoop();
  el.lockBtn.hidden = true;
  el.lockState.hidden = false;
  toast('Start position locked.');
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
// that single ordering is what tells it which way round the board is. Each
// handle marks a *corner of the chequered area*, not the middle of a square.
const CORNER_NAMES = ['a1', 'h1', 'h8', 'a8'];
const cornerState = [
  { x: 0.2, y: 0.3 }, { x: 0.8, y: 0.3 },
  { x: 0.85, y: 0.8 }, { x: 0.15, y: 0.8 },
];

const NS = 'http://www.w3.org/2000/svg';
let cornerNodes = [];          // built once; dragging only moves attributes

function openCornerPanel() {
  grabBlob().then((blob) => {
    if (blob) el.cornerStill.src = URL.createObjectURL(blob);
  });
  el.cornerPanel.hidden = false;
  buildHandles();
}

/**
 * Put the SVG exactly over the still, which `object-fit: contain` letterboxes
 * inside the stage. With the two aligned, a handle's 0..1 position *is* its
 * position in the photograph, and no unit conversion can drift.
 */
function syncCornerOverlay() {
  const iw = el.cornerStill.naturalWidth;
  const ih = el.cornerStill.naturalHeight;
  if (!iw || !ih) return;
  const box = el.cornerStill.getBoundingClientRect();
  const scale = Math.min(box.width / iw, box.height / ih);
  const w = iw * scale;
  const h = ih * scale;
  el.cornerSvg.style.width = `${w}px`;
  el.cornerSvg.style.height = `${h}px`;
  el.cornerSvg.style.left = `${(box.width - w) / 2}px`;
  el.cornerSvg.style.top = `${(box.height - h) / 2}px`;
}

function buildHandles() {
  el.cornerSvg.replaceChildren();
  cornerNodes = [];

  const poly = document.createElementNS(NS, 'polygon');
  poly.setAttribute('fill', 'rgba(143,164,116,0.15)');
  poly.setAttribute('stroke', '#8FA474');
  poly.setAttribute('stroke-width', '0.5');
  el.cornerSvg.appendChild(poly);

  cornerState.forEach((_, i) => {
    const ring = document.createElementNS(NS, 'circle');
    ring.setAttribute('r', '4');
    ring.setAttribute('fill', 'rgba(253,252,248,0.35)');
    ring.setAttribute('stroke', '#3E5A32');
    ring.setAttribute('stroke-width', '0.7');
    ring.style.cursor = 'grab';
    ring.dataset.index = String(i);

    // The ring is the thumb target; this dot is the point that actually lands
    // on the corner, so it must stay visible under a fingertip.
    const dot = document.createElementNS(NS, 'circle');
    dot.setAttribute('r', '0.9');
    dot.setAttribute('fill', '#A63826');
    dot.style.pointerEvents = 'none';

    const label = document.createElementNS(NS, 'text');
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('font-size', '3.4');
    label.setAttribute('font-weight', '600');
    label.setAttribute('fill', '#FDFCF8');
    label.setAttribute('stroke', '#211F1A');
    label.setAttribute('stroke-width', '0.6');
    label.setAttribute('paint-order', 'stroke');
    label.style.pointerEvents = 'none';
    label.textContent = CORNER_NAMES[i];

    el.cornerSvg.append(ring, dot, label);
    cornerNodes.push({ ring, dot, label, poly });
  });

  updateHandles();
  syncCornerOverlay();
}

function updateHandles() {
  const pts = cornerState.map((p) => `${p.x * 100},${p.y * 100}`).join(' ');
  cornerNodes.forEach(({ ring, dot, label, poly }, i) => {
    const p = cornerState[i];
    poly.setAttribute('points', pts);
    ring.setAttribute('cx', String(p.x * 100));
    ring.setAttribute('cy', String(p.y * 100));
    dot.setAttribute('cx', String(p.x * 100));
    dot.setAttribute('cy', String(p.y * 100));
    // Label sits clear of the ring so a finger never hides the aim point.
    label.setAttribute('x', String(p.x * 100));
    label.setAttribute('y', String(p.y * 100 - 5.5));
  });
}

// One listener on the SVG, which is never rebuilt mid-drag. The old code
// redrew every node on each pointermove, which destroyed the very circle
// holding the pointer capture — so a handle moved once per swipe and then
// went dead.
el.cornerSvg.addEventListener('pointerdown', (ev) => {
  const index = ev.target instanceof Element ? ev.target.dataset?.index : undefined;
  if (index === undefined) return;
  ev.preventDefault();
  const i = Number(index);
  el.cornerSvg.setPointerCapture(ev.pointerId);

  const move = (m) => {
    const box = el.cornerSvg.getBoundingClientRect();
    cornerState[i] = {
      x: Math.min(1, Math.max(0, (m.clientX - box.left) / box.width)),
      y: Math.min(1, Math.max(0, (m.clientY - box.top) / box.height)),
    };
    updateHandles();
  };
  const up = () => {
    el.cornerSvg.removeEventListener('pointermove', move);
    el.cornerSvg.removeEventListener('pointerup', up);
    el.cornerSvg.removeEventListener('pointercancel', up);
  };
  el.cornerSvg.addEventListener('pointermove', move);
  el.cornerSvg.addEventListener('pointerup', up);
  el.cornerSvg.addEventListener('pointercancel', up);
});

el.cornerStill.addEventListener('load', syncCornerOverlay);
window.addEventListener('resize', syncCornerOverlay);
window.addEventListener('orientationchange', () => setTimeout(syncCornerOverlay, 300));

el.cornerCancel.addEventListener('click', () => { el.cornerPanel.hidden = true; });
el.cornerUse.addEventListener('click', async () => {
  if (!game) {
    toast('No game to calibrate yet.');
    return;
  }
  // The handles are already in the photograph's own coordinates, because the
  // overlay is aligned to it, so this is a straight multiply.
  const iw = el.cornerStill.naturalWidth || LONG_EDGE;
  const ih = el.cornerStill.naturalHeight || LONG_EDGE;
  const corners = cornerState.map((p) => [
    Number((p.x * iw).toFixed(1)), Number((p.y * ih).toFixed(1))]);
  el.cornerUse.disabled = true;
  el.cornerUse.textContent = 'Saving…';
  try {
    await api(`/api/games/${game.game_id}/corners`, { method: 'POST', body: { corners } });
    manualCorners = true;
    el.cornerPanel.hidden = true;
    toast('Corners saved. They will not be overwritten.');
  } catch (err) {
    toast(`Could not save corners: ${err.message}`);
  } finally {
    el.cornerUse.disabled = false;
    el.cornerUse.textContent = 'Use these corners';
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
      el.lockBtn.hidden = true;
      startLocked = true;
      startBurst(msg.seq);
      break;
    case 'calibration':
      setDot(el.calDot, msg.ok ? 'ok' : (msg.warning ? 'warn' : 'error'));
      if (msg.ok && msg.corners) drawCalibration(msg.corners);
      // Never reopen over corners a player already placed by hand: the panel
      // popping back up is how they learn their work was thrown away.
      if (!msg.ok && msg.method !== 'manual' && !manualCorners) openCornerPanel();
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
el.lockBtn.addEventListener('click', lockStartFrame);

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
