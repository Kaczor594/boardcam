// Clock page: DOM + WebSocket wiring around the pure ClockSM state machine.
// Protocol: docs/PROTOCOL.md. Shared helpers: ws.js.

import { ClockSM } from './clocksm.js';
import {
  api,
  GameSocket,
  keepAwake,
  setDot,
  pulseDot,
  toast,
  modal,
  formatClock,
  roomFromUrl,
} from './ws.js';

/* ---------------- presets ---------------- */

const PRESETS = [
  { key: '3+2', label: '3+2', initialMs: 3 * 60000, incrementMs: 2000 },
  { key: '5+0', label: '5+0', initialMs: 5 * 60000, incrementMs: 0 },
  { key: '10+5', label: '10+5', initialMs: 10 * 60000, incrementMs: 5000 },
  { key: '15+10', label: '15+10', initialMs: 15 * 60000, incrementMs: 10000 },
];
const DEFAULT_PRESET = PRESETS[2]; // 10+5

/* ---------------- elements ---------------- */

const el = {
  joinScreen: document.getElementById('join-screen'),
  joinCode: document.getElementById('join-code'),
  joinBtn: document.getElementById('join-btn'),
  joinError: document.getElementById('join-error'),

  app: document.getElementById('app'),
  pairedDot: document.getElementById('paired-dot'),
  frameDot: document.getElementById('frame-dot'),
  frameLabel: document.getElementById('frame-label'),
  calDot: document.getElementById('cal-dot'),

  settings: document.getElementById('settings'),
  chipRow: document.getElementById('chip-row'),
  customChip: document.getElementById('chip-custom'),
  customFields: document.getElementById('custom-fields'),
  customMinutes: document.getElementById('custom-minutes'),
  customIncrement: document.getElementById('custom-increment'),

  zoneBlack: document.getElementById('zone-black'),
  zoneWhite: document.getElementById('zone-white'),
  timeBlack: document.getElementById('time-black'),
  timeWhite: document.getElementById('time-white'),
  flagBlack: document.getElementById('flag-black'),
  flagWhite: document.getElementById('flag-white'),

  pauseResumeBtn: document.getElementById('pause-resume-btn'),
  stopBtn: document.getElementById('stop-btn'),
  resetBtn: document.getElementById('reset-btn'),

  analysisPanel: document.getElementById('analysis-panel'),
  analysisPre: document.getElementById('analysis-pre'),
  copyPgnBtn: document.getElementById('copy-pgn-btn'),
  lichessLink: document.getElementById('lichess-link'),
  flaggedCount: document.getElementById('flagged-count'),
};

/* ---------------- state ---------------- */

let room = roomFromUrl();
let gameId = null;
let sock = null;
let selectedPreset = DEFAULT_PRESET;
const sm = new ClockSM({ initialMs: DEFAULT_PRESET.initialMs, incrementMs: DEFAULT_PRESET.incrementMs });

let audioCtx = null;

/* ---------------- audio (must be created inside a user gesture) --------- */

function ensureAudio() {
  if (audioCtx) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    audioCtx = new Ctx();
  } catch {
    audioCtx = null;
  }
}

function beep() {
  if (!audioCtx) return;
  if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = 'square';
  osc.frequency.value = 880;
  gain.gain.setValueAtTime(0.0001, audioCtx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.35);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start();
  osc.stop(audioCtx.currentTime + 0.4);
}

document.addEventListener('pointerdown', ensureAudio, { once: true });

/* ---------------- join flow ---------------- */

function showJoinError(msg) {
  el.joinError.textContent = msg;
  el.joinError.hidden = !msg;
}

async function resolveRoom(code) {
  showJoinError('');
  el.joinBtn.disabled = true;
  try {
    const data = await api(`/api/games/by-room/${encodeURIComponent(code)}`);
    gameId = data.game_id;
    room = code;
    if (data.initial_ms && data.increment_ms !== undefined && sm.state === 'ready') {
      sm.configure({ initialMs: data.initial_ms, incrementMs: data.increment_ms });
      selectPresetForConfig(data.initial_ms, data.increment_ms);
    }
    el.joinScreen.hidden = true;
    el.app.hidden = false;
    connectSocket();
  } catch (err) {
    if (err.status === 404) {
      showJoinError('No game found for that room code.');
    } else {
      showJoinError('Could not reach the server. Try again.');
    }
  } finally {
    el.joinBtn.disabled = false;
  }
}

el.joinBtn.addEventListener('click', () => {
  const code = el.joinCode.value.trim().toUpperCase();
  if (code.length !== 4) {
    showJoinError('Enter the 4-character room code.');
    return;
  }
  resolveRoom(code);
});

el.joinCode.addEventListener('input', () => {
  el.joinCode.value = el.joinCode.value.toUpperCase();
});

/* ---------------- socket ---------------- */

function connectSocket() {
  sock = new GameSocket(room, 'clock');
  sock.onstate = (s) => {
    if (s === 'closed') setDot(el.pairedDot, null);
  };
  sock.onmessage = handleServerMessage;
  sock.connect();
}

function send(msg) {
  if (sock) sock.send(msg);
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case 'hello': {
      // Restore status only; never rewind a running local clock. The clock
      // phone's own ClockSM is authoritative for time.
      if (msg.config && sm.state === 'ready') {
        sm.configure({ initialMs: msg.config.initial_ms, incrementMs: msg.config.increment_ms });
        selectPresetForConfig(msg.config.initial_ms, msg.config.increment_ms);
      }
      // The state machine restarts at seq 0 on a reload, but the game's frames
      // are already numbered on disk. Take the server's count as a floor so a
      // reconnected clock writes new frames after them, never over them.
      if (typeof msg.seq === 'number') {
        seqBase = Math.max(seqBase, msg.seq - sm.seq);
      }
      setDot(el.pairedDot, msg.peer_connected ? 'ok' : null);
      break;
    }
    case 'peer': {
      if (msg.role === 'camera') setDot(el.pairedDot, msg.connected ? 'ok' : null);
      break;
    }
    case 'frame.ok': {
      el.frameLabel.textContent = `frame #${msg.seq}`;
      pulseDot(el.frameDot);
      break;
    }
    case 'calibration': {
      setDot(el.calDot, msg.ok ? 'ok' : (msg.warning ? 'warn' : 'error'));
      break;
    }
    case 'analysis.ready': {
      showAnalysis(msg);
      break;
    }
    default:
      break; // unknown types are ignored per PROTOCOL.md
  }
}

function showAnalysis(msg) {
  el.analysisPanel.hidden = false;
  el.analysisPre.textContent = msg.pgn || '';
  if (msg.lichess_url) {
    el.lichessLink.href = msg.lichess_url;
    el.lichessLink.hidden = false;
  } else {
    el.lichessLink.hidden = true;
  }
  const flaggedCount = Array.isArray(msg.flagged) ? msg.flagged.length : (msg.flagged || 0);
  el.flaggedCount.textContent = `${flaggedCount} flagged ply${flaggedCount === 1 ? '' : 's'}`;
}

el.copyPgnBtn.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(el.analysisPre.textContent);
    toast('PGN copied');
  } catch {
    toast('Could not copy PGN');
  }
});

/* ---------------- settings ---------------- */

const presetButtons = new Map(); // preset -> button element

function buildChips() {
  el.chipRow.innerHTML = '';
  presetButtons.clear();
  for (const preset of PRESETS) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip';
    btn.textContent = preset.label;
    btn.setAttribute('aria-pressed', String(preset === selectedPreset));
    btn.addEventListener('click', () => applyPreset(preset));
    presetButtons.set(preset, btn);
    el.chipRow.appendChild(btn);
  }
  el.chipRow.appendChild(el.customChip);
}

function refreshChipPressed() {
  presetButtons.forEach((btn, preset) => {
    btn.setAttribute('aria-pressed', String(preset === selectedPreset));
  });
  el.customChip.setAttribute('aria-pressed', String(selectedPreset === 'custom'));
  el.customFields.hidden = selectedPreset !== 'custom';
}

function applyPreset(preset) {
  if (sm.state !== 'ready') return;
  selectedPreset = preset;
  sm.configure({ initialMs: preset.initialMs, incrementMs: preset.incrementMs });
  refreshChipPressed();
  sendConfig();
}

el.customChip.addEventListener('click', () => {
  if (sm.state !== 'ready') return;
  selectedPreset = 'custom';
  refreshChipPressed();
  el.customMinutes.focus();
});

function applyCustom() {
  if (sm.state !== 'ready') return;
  const minutes = Number(el.customMinutes.value);
  const seconds = Number(el.customIncrement.value);
  if (!Number.isFinite(minutes) || minutes <= 0 || !Number.isFinite(seconds) || seconds < 0) return;
  selectedPreset = 'custom';
  sm.configure({ initialMs: minutes * 60000, incrementMs: seconds * 1000 });
  refreshChipPressed();
  sendConfig();
}

el.customMinutes.addEventListener('change', applyCustom);
el.customIncrement.addEventListener('change', applyCustom);

function selectPresetForConfig(initialMs, incrementMs) {
  const match = PRESETS.find((p) => p.initialMs === initialMs && p.incrementMs === incrementMs);
  if (match) {
    selectedPreset = match;
  } else {
    selectedPreset = 'custom';
    el.customMinutes.value = String(Math.round(initialMs / 60000));
    el.customIncrement.value = String(Math.round(incrementMs / 1000));
  }
  refreshChipPressed();
}

function sendConfig() {
  send({
    type: 'clock.config',
    initial_ms: sm.initialMs,
    increment_ms: sm.incrementMs,
    white_name: 'White',
    black_name: 'Black',
  });
}

buildChips();
refreshChipPressed();

/* ---------------- pressing ---------------- */

// PROTOCOL.md §4: flag and stop also make the camera capture, because the last
// move of a game is often played without pressing the clock. Those events are
// not presses, so they cannot take their seq from the state machine — they
// continue the same numbering from here.
let lastCaptureSeq = 0;
let seqBase = 0;   // offset applied to the state machine's press count

function wireSeq(smSeq) {
  return seqBase + smSeq;
}

function nextCaptureSeq() {
  lastCaptureSeq = Math.max(lastCaptureSeq, wireSeq(sm.seq)) + 1;
  return lastCaptureSeq;
}

function attemptPress(side) {
  const now = performance.now();
  const res = sm.press(side, now);
  if (!res) return;
  const seq = wireSeq(res.seq);
  lastCaptureSeq = seq;

  if (res.started) {
    send({
      type: 'clock.start',
      seq,
      side: res.side,
      white_ms: res.whiteMs,
      black_ms: res.blackMs,
      t: Date.now(),
    });
  } else {
    send({
      type: 'clock.press',
      seq,
      side: res.side,
      white_ms: res.whiteMs,
      black_ms: res.blackMs,
      t: Date.now(),
    });
  }
  updateSettingsVisibility();
}

el.zoneWhite.addEventListener('pointerdown', (e) => { e.preventDefault(); attemptPress('white'); });
el.zoneBlack.addEventListener('pointerdown', (e) => { e.preventDefault(); attemptPress('black'); });

/* ---------------- controls ---------------- */

el.pauseResumeBtn.addEventListener('click', () => {
  const now = performance.now();
  if (sm.state === 'running') {
    if (sm.pause(now)) send({ type: 'clock.pause', t: Date.now() });
  } else if (sm.state === 'paused') {
    if (sm.resume(now)) send({ type: 'clock.resume', t: Date.now() });
  }
});

el.stopBtn.addEventListener('click', async () => {
  const value = await modal({
    title: 'End game',
    body: 'Record the result.',
    row: true,
    choices: [
      { label: '1-0', value: '1-0' },
      { label: '0-1', value: '0-1' },
      { label: '½-½', value: '1/2-1/2' },
    ],
  });
  if (!value) return;
  const now = performance.now();
  if (sm.stop(value, now)) {
    send({
      type: 'clock.stop',
      seq: nextCaptureSeq(),
      result: value,
      white_ms: sm.remaining('white', now),
      black_ms: sm.remaining('black', now),
      t: Date.now(),
    });
  }
});

el.resetBtn.addEventListener('click', async () => {
  const value = await modal({
    title: 'Reset clock',
    body: 'This ends the current game. A new game must be created to play again.',
    choices: [
      { label: 'Cancel', value: null },
      { label: 'Reset', value: true, cls: 'btn-danger' },
    ],
  });
  if (!value) return;
  const now = performance.now();
  sm.reset(now);
  lastCaptureSeq = 0;
  seqBase = 0;
  send({ type: 'clock.reset', t: Date.now() });
  updateSettingsVisibility();
});

function updateSettingsVisibility() {
  el.settings.hidden = sm.state !== 'ready';
}

/* ---------------- render loop ---------------- */

let flaggedBeeped = false;

function render(now) {
  const wm = sm.remaining('white', now);
  const bm = sm.remaining('black', now);
  el.timeWhite.textContent = formatClock(wm);
  el.timeBlack.textContent = formatClock(bm);

  const running = sm.state === 'running';
  const whiteActive = running && sm.turn === 'white';
  const blackActive = running && sm.turn === 'black';

  el.zoneWhite.classList.toggle('active', whiteActive);
  el.zoneWhite.classList.toggle('idle', running && !whiteActive);
  el.zoneBlack.classList.toggle('active', blackActive);
  el.zoneBlack.classList.toggle('idle', running && !blackActive);

  const flaggedWhite = sm.state === 'flagged' && sm.flaggedSide === 'white';
  const flaggedBlack = sm.state === 'flagged' && sm.flaggedSide === 'black';
  el.zoneWhite.classList.toggle('flagged', flaggedWhite);
  el.zoneBlack.classList.toggle('flagged', flaggedBlack);
  el.flagWhite.hidden = !flaggedWhite;
  el.flagBlack.hidden = !flaggedBlack;

  if (sm.state === 'flagged' && !flaggedBeeped) {
    flaggedBeeped = true;
    beep();
    send({
      type: 'clock.flag',
      seq: nextCaptureSeq(),
      side: sm.flaggedSide,
      white_ms: sm.remaining('white', now),
      black_ms: sm.remaining('black', now),
      t: Date.now(),
    });
  }
  if (sm.state !== 'flagged') flaggedBeeped = false;

  el.pauseResumeBtn.textContent = sm.state === 'paused' ? 'Resume' : 'Pause';
  el.pauseResumeBtn.disabled = sm.state !== 'running' && sm.state !== 'paused';
  el.stopBtn.disabled = sm.state === 'ready' || sm.state === 'stopped';
}

function loop(ts) {
  // tick() drives ready->flagged transitions from elapsed time alone; render()
  // below reacts to sm.state === 'flagged' and sends clock.flag exactly once.
  sm.tick(ts);
  render(ts);
  requestAnimationFrame(loop);
}

/* ---------------- boot ---------------- */

function boot() {
  keepAwake();
  updateSettingsVisibility();
  if (room) {
    el.joinCode.value = room;
    resolveRoom(room);
  } else {
    el.joinScreen.hidden = false;
    el.app.hidden = true;
  }
  requestAnimationFrame(loop);
}

boot();
