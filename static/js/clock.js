// Clock page: one phone is the clock and the camera.
// Pure clock logic lives in clocksm.js, the board editor in board.js, shared
// network helpers in ws.js. Protocol: docs/PROTOCOL.md.

import { ClockSM, photoHint } from './clocksm.js';
import { BoardEditor } from './board.js';
import {
  api,
  GameSocket,
  keepAwake,
  setDot,
  toast,
  modal,
  formatClock,
} from './ws.js';

/* ---------------- presets ---------------- */

const PRESETS = [
  { key: '3+2', label: '3+2', initialMs: 3 * 60000, incrementMs: 2000 },
  { key: '5+0', label: '5+0', initialMs: 5 * 60000, incrementMs: 0 },
  { key: '10+5', label: '10+5', initialMs: 10 * 60000, incrementMs: 5000 },
  { key: '15+10', label: '15+10', initialMs: 15 * 60000, incrementMs: 10000 },
];

const READER_LABELS = {
  'claude-fable-5-2': ['Claude Fable 5.2', 'Most accurate: 99.6 % of squares on the hold-out photos. About $0.20 and a minute a photo; a photo taken across a corner can take three minutes.'],
  'claude-sonnet-5-5': ['Claude Sonnet 5.5', 'Cheaper (about $0.04 a photo), 98.9 % of squares in the pilot.'],
  'claude-opus-5-5': ['Claude Opus 5.5', '95.3 % of squares in the pilot. Kept for comparison.'],
};

/* ---------------- elements ---------------- */

const $ = (id) => document.getElementById(id);
const el = {
  setup: $('setup'),
  chipRow: $('chip-row'),
  customChip: $('chip-custom'),
  customFields: $('custom-fields'),
  customMinutes: $('custom-minutes'),
  customIncrement: $('custom-increment'),
  whiteName: $('white-name'),
  blackName: $('black-name'),
  whiteRating: $('white-rating'),
  blackRating: $('black-rating'),
  readerModel: $('reader-model'),
  readerNote: $('reader-note'),
  startBtn: $('start-btn'),
  setupError: $('setup-error'),

  app: $('app'),
  connDot: $('conn-dot'),
  plyLabel: $('ply-label'),
  photoDot: $('photo-dot'),
  photosLabel: $('photos-label'),

  zoneBlack: $('zone-black'),
  zoneWhite: $('zone-white'),
  timeBlack: $('time-black'),
  timeWhite: $('time-white'),
  flagBlack: $('flag-black'),
  flagWhite: $('flag-white'),

  pauseResumeBtn: $('pause-resume-btn'),
  cameraBtn: $('camera-btn'),
  stopBtn: $('stop-btn'),
  newBtn: $('new-btn'),
  cameraInput: $('camera-input'),
  finishedBanner: $('finished-banner'),

  sheet: $('photo-sheet'),
  sheetTitle: $('sheet-title'),
  sheetMeta: $('sheet-meta'),
  sheetThumb: $('sheet-thumb'),
  sheetReading: $('sheet-reading'),
  sheetError: $('sheet-error'),
  sheetEdit: $('sheet-edit'),
  sheetFlags: $('sheet-flags'),
  sheetBoard: $('sheet-board'),
  sheetPly: $('sheet-ply'),
  plyMinus: $('ply-minus'),
  plyPlus: $('ply-plus'),
  sheetRotate: $('sheet-rotate'),
  sheetUndo: $('sheet-undo'),
  sheetResume: $('sheet-resume'),
  sheetConfirm: $('sheet-confirm'),
  sheetRetry: $('sheet-retry'),
  sheetRetake: $('sheet-retake'),
  sheetManual: $('sheet-manual'),
  sheetLater: $('sheet-later'),
};

/* ---------------- state ---------------- */

let game = null;              // {game_id, room}
let sock = null;
let selectedPreset = PRESETS[2];
let sm = new ClockSM({ initialMs: selectedPreset.initialMs, incrementMs: selectedPreset.incrementMs });
let seqBase = 0;              // floor from the server if local state was lost
let flagSent = false;
let finishedNotice = false;
let hintPlies = null;         // hello.hint_plies: glow the camera button past this many plies
let photos = [];              // positions.json "photos", newest last
let sheet = null;             // {n, file?, objectUrl?, editor, ply, retakeOf, stamp}

/* ---------------- persistence (survives iOS discarding the page) -------- */

const ACTIVE_KEY = 'boardcam.game';
const clockKey = (gid) => `boardcam.clock.${gid}`;

function store(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* private mode or full: the clock still works, it just won't survive a reload */ }
}

function load(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}

/** Saved on every change of clock state, so a reload loses nothing. */
function save() {
  if (!game) return;
  store(ACTIVE_KEY, game);
  store(clockKey(game.game_id), {
    snap: sm.snapshot(performance.now(), Date.now()),
    seqBase,
    flagSent,
  });
}

function forgetGame() {
  if (game) store(clockKey(game.game_id), null);
  store(ACTIVE_KEY, null);
}

/* ---------------- audio (must be created inside a user gesture) --------- */

let audioCtx = null;
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

/* ---------------- setup screen ---------------- */

const presetButtons = new Map();

function buildChips() {
  el.chipRow.innerHTML = '';
  presetButtons.clear();
  for (const preset of PRESETS) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip';
    btn.textContent = preset.label;
    btn.addEventListener('click', () => { selectedPreset = preset; refreshChips(); });
    presetButtons.set(preset, btn);
    el.chipRow.appendChild(btn);
  }
  el.chipRow.appendChild(el.customChip);
}

function refreshChips() {
  presetButtons.forEach((btn, preset) => btn.setAttribute('aria-pressed', String(preset === selectedPreset)));
  el.customChip.setAttribute('aria-pressed', String(selectedPreset === 'custom'));
  el.customFields.hidden = selectedPreset !== 'custom';
}

el.customChip.addEventListener('click', () => {
  selectedPreset = 'custom';
  refreshChips();
  el.customMinutes.focus();
});

function selectPresetFor(initialMs, incrementMs) {
  const match = PRESETS.find((p) => p.initialMs === initialMs && p.incrementMs === incrementMs);
  if (match) {
    selectedPreset = match;
  } else {
    selectedPreset = 'custom';
    el.customMinutes.value = String(Math.round(initialMs / 60000));
    el.customIncrement.value = String(Math.round(incrementMs / 1000));
  }
  refreshChips();
}

function timeControl() {
  if (selectedPreset !== 'custom') {
    return { initial_ms: selectedPreset.initialMs, increment_ms: selectedPreset.incrementMs };
  }
  const minutes = Number(el.customMinutes.value);
  const seconds = Number(el.customIncrement.value);
  if (!(minutes > 0) || !(seconds >= 0)) return null;
  return { initial_ms: Math.round(minutes * 60000), increment_ms: Math.round(seconds * 1000) };
}

function showReaderNote() {
  const entry = READER_LABELS[el.readerModel.value];
  el.readerNote.textContent = entry ? entry[1] : '';
}
el.readerModel.addEventListener('change', showReaderNote);

async function showSetup() {
  el.app.hidden = true;
  el.setup.hidden = false;
  el.setupError.hidden = true;
  buildChips();
  let defaults = {};
  let models = Object.keys(READER_LABELS);
  try {
    const got = await api('/api/settings');
    defaults = got.defaults;
    models = got.reader_models;
  } catch {
    el.setupError.textContent = 'Could not reach the server. Check the tunnel, then reload.';
    el.setupError.hidden = false;
  }
  el.readerModel.innerHTML = '';
  for (const m of models) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = (READER_LABELS[m] || [m])[0];
    el.readerModel.appendChild(opt);
  }
  el.readerModel.value = defaults.reader_model || models[0];
  showReaderNote();
  el.whiteName.value = defaults.white_name || 'White';
  el.blackName.value = defaults.black_name || 'Black';
  el.whiteRating.value = String(defaults.white_rating || 1350);
  el.blackRating.value = String(defaults.black_rating || 1350);
  selectPresetFor(defaults.initial_ms || 600000, defaults.increment_ms ?? 5000);
}

el.startBtn.addEventListener('click', async () => {
  const tc = timeControl();
  const wr = Number(el.whiteRating.value);
  const br = Number(el.blackRating.value);
  const bad = !tc ? 'Enter minutes above 0 and an increment of 0 or more.'
    : !(wr >= 100 && wr <= 3500 && br >= 100 && br <= 3500) ? 'Ratings must be between 100 and 3500.'
      : null;
  if (bad) {
    el.setupError.textContent = bad;
    el.setupError.hidden = false;
    return;
  }
  el.startBtn.disabled = true;
  try {
    const created = await api('/games', {
      method: 'POST',
      body: {
        ...tc,
        white_name: el.whiteName.value.trim() || 'White',
        black_name: el.blackName.value.trim() || 'Black',
        white_rating: wr,
        black_rating: br,
        reader_model: el.readerModel.value,
      },
    });
    startGame({ game_id: created.game_id, room: created.room },
      new ClockSM({ initialMs: created.initial_ms, incrementMs: created.increment_ms }));
  } catch (err) {
    el.setupError.textContent = `Could not create the game (${err.message}).`;
    el.setupError.hidden = false;
  } finally {
    el.startBtn.disabled = false;
  }
});

/* ---------------- game lifecycle ---------------- */

function startGame(g, clock, saved = null) {
  game = g;
  sm = clock;
  seqBase = saved ? saved.seqBase || 0 : 0;
  flagSent = saved ? Boolean(saved.flagSent) : false;
  finishedNotice = false;
  el.finishedBanner.hidden = true;
  photos = [];
  save();
  el.setup.hidden = true;
  el.app.hidden = false;
  connectSocket();
  refreshPhotos();
  if (sm.state === 'stopped' || sm.state === 'flagged') gameIsOver(sm.result);
}

async function resumeSavedGame() {
  const g = load(ACTIVE_KEY);
  if (!g || !g.game_id) return false;
  let meta;
  try {
    meta = await api(`/api/games/${g.game_id}`);
  } catch (err) {
    if (err.status === 404) { store(ACTIVE_KEY, null); return false; }
    // Server unreachable: still restore the clock; the game must go on.
  }
  const saved = load(clockKey(g.game_id));
  let clock = saved ? ClockSM.restore(saved.snap, performance.now(), Date.now()) : null;
  if (!clock) {
    clock = new ClockSM({
      initialMs: meta ? meta.initial_ms : 600000,
      incrementMs: meta ? meta.increment_ms : 5000,
    });
  }
  startGame({ game_id: g.game_id, room: g.room || (meta && meta.room) }, clock, saved);
  if (saved && saved.snap && saved.snap.state === 'running') toast('Clock restored after a reload');
  return true;
}

function gameIsOver(result) {
  if (finishedNotice) return;
  finishedNotice = true;
  el.finishedBanner.hidden = false;
  const res = result ? ` (${result})` : '';
  el.finishedBanner.innerHTML = '';
  el.finishedBanner.append(`This game is finished${res}. Take a photo of the final position, then `);
  const a = document.createElement('a');
  a.href = `/review?game=${encodeURIComponent(game.game_id)}`;
  a.textContent = 'review it';
  el.finishedBanner.append(a, '.');
}

/* ---------------- socket ---------------- */

function connectSocket() {
  if (sock) sock.close();
  if (!game.room) return;
  sock = new GameSocket(game.room, 'clock');
  sock.onstate = (s) => setDot(el.connDot, s === 'open' ? 'ok' : (s === 'closed' ? 'error' : null));
  sock.onmessage = handleServerMessage;
  sock.connect();
}

function send(msg) {
  if (sock) sock.send(msg);
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case 'hello': {
      // The local clock is authoritative for time. The server's press count is
      // only a floor, for when this phone lost its saved state.
      if (typeof msg.seq === 'number' && msg.seq > wireSeq(sm.seq) && sm.state !== 'ready') {
        seqBase = msg.seq - sm.seq;
        save();
      }
      if (msg.status === 'finished') gameIsOver(msg.result);
      hintPlies = msg.hint_plies ?? null;
      refreshPhotos();
      break;
    }
    case 'position.read': {
      if (msg.game_id && msg.game_id !== game.game_id) break;
      upsertPhoto(msg.photo);
      if (sheet && sheet.n === msg.photo.n) showSheetFor(msg.photo);
      break;
    }
    case 'game.finished':
      gameIsOver(msg.result);
      break;
    default:
      break; // unknown types are ignored per PROTOCOL.md
  }
}

/* ---------------- pressing ---------------- */

function wireSeq(smSeq) {
  return seqBase + smSeq;
}

function attemptPress(side) {
  const res = sm.press(side, performance.now());
  if (!res) return;
  send({
    type: res.started ? 'clock.start' : 'clock.press',
    seq: wireSeq(res.seq),
    side: res.side,
    white_ms: res.whiteMs,
    black_ms: res.blackMs,
    t: Date.now(),
  });
  save();
}

el.zoneWhite.addEventListener('pointerdown', (e) => { e.preventDefault(); attemptPress('white'); });
el.zoneBlack.addEventListener('pointerdown', (e) => { e.preventDefault(); attemptPress('black'); });

function pauseClock() {
  if (sm.pause(performance.now())) {
    send({ type: 'clock.pause', t: Date.now() });
    save();
  }
}

function resumeClock() {
  if (sm.resume(performance.now())) {
    send({ type: 'clock.resume', t: Date.now() });
    save();
  }
}

el.pauseResumeBtn.addEventListener('click', () => {
  if (sm.state === 'running') pauseClock();
  else if (sm.state === 'paused') resumeClock();
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
      ply: wireSeq(sm.seq),
      result: value,
      white_ms: sm.remaining('white', now),
      black_ms: sm.remaining('black', now),
      t: Date.now(),
    });
    save();
    gameIsOver(value);
  }
});

el.newBtn.addEventListener('click', async () => {
  const inPlay = sm.state === 'running' || sm.state === 'paused';
  const value = await modal({
    title: 'New game',
    body: inPlay
      ? 'This game has not been stopped. It is kept with no result, and its photos stay on the server.'
      : 'Leave this game and set up a new one. Its photos stay on the server.',
    choices: [
      { label: 'Cancel', value: null },
      { label: 'New game', value: true, cls: inPlay ? 'btn-danger' : 'btn-primary' },
    ],
  });
  if (!value) return;
  if (inPlay) {
    const now = performance.now();
    sm.stop('*', now);
    send({
      type: 'clock.stop', ply: wireSeq(sm.seq), result: '*',
      white_ms: sm.remaining('white', now), black_ms: sm.remaining('black', now), t: Date.now(),
    });
  }
  forgetGame();
  if (sock) sock.close();
  sock = null;
  game = null;
  closeSheet();
  el.finishedBanner.hidden = true;
  showSetup();
});

/* ---------------- photos ---------------- */

function upsertPhoto(p) {
  const i = photos.findIndex((x) => x.n === p.n);
  if (i >= 0) photos[i] = p; else photos.push(p);
  photos.sort((a, b) => a.n - b.n);
  if (p.replaced_by == null && p.retake_of != null) {
    const old = photos.find((x) => x.n === p.retake_of);
    if (old) old.status = 'replaced';
  }
  showPhotoStatus();
}

async function refreshPhotos() {
  if (!game) return;
  try {
    const doc = await api(`/api/games/${game.game_id}/positions`);
    photos = doc.photos || [];
    showPhotoStatus();
    if (sheet && sheet.n != null) {
      const p = photos.find((x) => x.n === sheet.n);
      if (p) showSheetFor(p);
    }
  } catch { /* offline: the strip just stays as it was */ }
}

const live = () => photos.filter((p) => p.status !== 'replaced');
const toConfirm = () => live().filter((p) => p.status === 'read' || p.status === 'error');

function showPhotoStatus() {
  const all = live();
  const open = toConfirm().length;
  const reading = all.some((p) => p.status === 'reading');
  if (!all.length) {
    el.photosLabel.textContent = 'no photos';
  } else {
    el.photosLabel.textContent = `${all.length} photo${all.length === 1 ? '' : 's'}`
      + (open ? ` · ${open} to confirm` : '') + (reading ? ' · reading' : '');
  }
  setDot(el.photoDot, reading ? 'warn' : open ? 'error' : all.length ? 'ok' : null);
}

el.photosLabel.addEventListener('click', () => {
  if (sm.state === 'running') {
    toast('Pause the clock to look at photos');
    return;
  }
  const p = toConfirm().at(-1) || live().at(-1);
  if (p) openSheet(p.n);
});

/** The clock's state at the moment the photo is taken: ply, side to move, times. */
function stampNow() {
  const now = performance.now();
  const ply = wireSeq(sm.seq);
  return {
    ply,
    turn: sm.turn || (ply % 2 === 0 ? 'white' : 'black'),
    white_ms: Math.round(sm.remaining('white', now)),
    black_ms: Math.round(sm.remaining('black', now)),
  };
}

let pendingShot = null;   // {stamp, retakeOf}

function takePhoto(retakeOf = null, stamp = null) {
  // Pause first, inside the tap, so the time spent framing the shot is nobody's.
  if (sm.state === 'running') pauseClock();
  pendingShot = { stamp: stamp || stampNow(), retakeOf };
  el.cameraInput.value = '';
  el.cameraInput.click();   // must stay inside the user gesture on iOS
}

el.cameraBtn.addEventListener('click', () => {
  if (!game) return;
  takePhoto();
});

el.cameraInput.addEventListener('change', () => {
  const file = el.cameraInput.files && el.cameraInput.files[0];
  if (!file || !pendingShot) return;   // cancelled: the clock just stays paused
  const shot = pendingShot;
  pendingShot = null;
  uploadPhoto(file, shot);
});

async function uploadPhoto(file, { stamp, retakeOf }) {
  const objectUrl = URL.createObjectURL(file);
  openSheet(null, { objectUrl, stamp, uploading: true });
  const form = new FormData();
  form.append('file', file, file.name || 'photo.jpg');
  form.append('ply', String(stamp.ply));
  if (stamp.turn) form.append('turn', stamp.turn);
  form.append('white_ms', String(stamp.white_ms));
  form.append('black_ms', String(stamp.black_ms));
  if (retakeOf != null) form.append('retake_of', String(retakeOf));
  const knownN = Math.max(0, ...photos.map((p) => p.n));
  try {
    const entry = await api(`/games/${game.game_id}/photos`, { method: 'POST', body: form });
    upsertPhoto(entry);
    if (sheet && sheet.objectUrl === objectUrl) {
      sheet.n = entry.n;
      showSheetFor(entry);
    }
  } catch (err) {
    // The read may still be running on the server even though this request
    // died (tunnel blip, page backgrounded). Look before calling it lost.
    await refreshPhotos();
    const landed = photos.filter((p) => p.n > knownN).at(-1);
    if (landed && sheet && sheet.objectUrl === objectUrl) {
      sheet.n = landed.n;
      showSheetFor(landed);
      pollWhileReading(landed.n);
    } else if (sheet && sheet.objectUrl === objectUrl) {
      sheet.uploading = false;
      sheet.failedFile = file;
      sheet.failedShot = { stamp, retakeOf };
      showUploadFailed(err);
    }
  }
}

async function pollWhileReading(n) {
  for (let i = 0; i < 120; i += 1) {
    await new Promise((r) => setTimeout(r, 4000));
    if (!sheet || sheet.n !== n) return;
    await refreshPhotos();
    const p = photos.find((x) => x.n === n);
    if (!p || p.status !== 'reading') return;
  }
}

/* ---------------- photo sheet ---------------- */

let readingTimer = null;

function openSheet(n, extra = {}) {
  sheet = { n, editor: null, ply: null, ...extra };
  el.sheet.hidden = false;
  el.sheetThumb.src = extra.objectUrl
    || (n != null ? `/games/${game.game_id}/photos/${n}` : '');
  if (n != null) {
    const p = photos.find((x) => x.n === n);
    if (p) showSheetFor(p);
  } else {
    showReading(extra.stamp, 'Uploading the photo…');
  }
  refreshSheetButtons();
}

function closeSheet() {
  if (sheet && sheet.objectUrl) URL.revokeObjectURL(sheet.objectUrl);
  sheet = null;
  el.sheet.hidden = true;
  clearInterval(readingTimer);
  readingTimer = null;
}

function describeStamp(stamp) {
  const side = stamp.ply % 2 === 0 ? 'White' : 'Black';
  return `ply ${stamp.ply} · ${side} to move`;
}

function setSections({ reading = false, error = false, edit = false }) {
  el.sheetReading.hidden = !reading;
  el.sheetError.hidden = !error;
  el.sheetEdit.hidden = !edit;
  if (!reading) { clearInterval(readingTimer); readingTimer = null; }
}

function showReading(stamp, text) {
  el.sheetTitle.textContent = sheet.n ? `Photo ${sheet.n}` : 'Photo';
  el.sheetMeta.textContent = stamp ? describeStamp(stamp) : '';
  setSections({ reading: true });
  const t0 = Date.now();
  const paint = () => {
    const s = Math.round((Date.now() - t0) / 1000);
    el.sheetReading.textContent = `${text} ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}.`
      + (s > 45 ? ' A photo taken across a corner can take three minutes. You can resume play; the board waits for you here.' : '');
  };
  paint();
  clearInterval(readingTimer);
  readingTimer = setInterval(paint, 1000);
  el.sheetConfirm.hidden = true;
  el.sheetRetry.hidden = true;
  el.sheetManual.hidden = true;
}

function showUploadFailed(err) {
  setSections({ error: true });
  el.sheetError.textContent = `The photo did not reach the server (${err.message}). Send it again, or retake it.`;
  el.sheetRetry.hidden = false;
  el.sheetRetry.textContent = 'Send again';
  el.sheetConfirm.hidden = true;
  el.sheetManual.hidden = true;
}

const ERROR_TEXT = {
  no_board: 'No board was found in the photo.',
  refusal: 'The reader declined this photo.',
  max_tokens: 'The reader ran out of room before answering. A photo from one side of the board is quicker to read.',
  bad_shape: 'The reader returned something that is not an 8×8 board.',
  bad_symbol: 'The reader returned a piece it should not have.',
  rate_limited: 'The reader is busy. Try reading again in a minute.',
  connection: 'The server could not reach the reader.',
  server_error: 'The server failed while reading this photo.',
};

function showSheetFor(p) {
  if (!sheet) return;
  sheet.n = p.n;
  sheet.uploading = false;
  el.sheetTitle.textContent = `Photo ${p.n}`;
  if (!sheet.objectUrl) el.sheetThumb.src = `/games/${game.game_id}/photos/${p.n}`;
  el.sheetMeta.textContent = `clock at ${describeStamp({ ply: p.ply })} · ${p.model.replace('claude-', '')}`;
  el.sheetRetry.textContent = 'Read again';

  if (p.status === 'reading') {
    showReading(p, 'Reading the board…');
    return;
  }
  if (p.status === 'replaced') {
    setSections({ error: true });
    el.sheetError.textContent = `This photo was replaced by photo ${p.replaced_by}.`;
    el.sheetConfirm.hidden = true;
    el.sheetRetry.hidden = true;
    el.sheetManual.hidden = true;
    return;
  }
  if (p.status === 'error' && !sheet.manual) {
    setSections({ error: true });
    el.sheetError.textContent = `${ERROR_TEXT[p.error.reason] || 'The read failed.'} (${p.error.reason})`;
    el.sheetRetry.hidden = !p.error.retry;
    el.sheetManual.hidden = false;
    el.sheetConfirm.hidden = true;
    return;
  }

  // read, confirmed, or error being entered by hand: the editor.
  setSections({ edit: true });
  el.sheetRetry.hidden = true;
  el.sheetManual.hidden = true;
  el.sheetConfirm.hidden = false;
  if (!sheet.editor || sheet.editorFor !== `${p.n}:${p.status}`) {
    const base = p.status === 'confirmed' ? p.confirmed : (p.read || { rows: null, home: 'bottom' });
    sheet.editor = new BoardEditor(el.sheetBoard, {
      rows: base.rows, home: base.home, editable: true, onChange: () => showFlags(null),
    });
    sheet.editorFor = `${p.n}:${p.status}`;
    sheet.ply = p.status === 'confirmed' ? p.confirmed.ply : p.ply;
    sheet.clockPly = p.ply;
    showFlags(p.status === 'read' && p.position && !p.position.legal ? p.position.flags : null);
  }
  el.sheetConfirm.textContent = p.status === 'confirmed' ? 'Save changes' : 'Confirm board';
  showPly();
}

const FLAG_TEXT = {
  no_white_king: 'no white king',
  no_black_king: 'no black king',
  too_many_kings: 'more than one king a side',
  too_many_white_pawns: 'more than 8 white pawns',
  too_many_black_pawns: 'more than 8 black pawns',
  pawns_on_back_rank: 'a pawn on the first or last rank',
  too_many_white_pieces: 'more than 16 white men',
  too_many_black_pieces: 'more than 16 black men',
  side_not_to_move_in_check: 'the side that just moved is in check',
  too_many_checkers: 'more than two pieces give check',
  impossible_check: 'a check no move could have given',
};

function showFlags(flags) {
  if (!flags || !flags.length) {
    el.sheetFlags.hidden = true;
    return;
  }
  el.sheetFlags.hidden = false;
  el.sheetFlags.textContent = `This position is impossible: ${flags.map((f) => FLAG_TEXT[f] || f).join('; ')}. `
    + 'Fix the squares, rotate the board, change the ply, or retake the photo.';
}

function showPly() {
  const ply = sheet.ply;
  el.sheetPly.textContent = `ply ${ply}`;
  el.sheetPly.title = ply % 2 === 0 ? 'White to move' : 'Black to move';
  el.plyMinus.disabled = ply <= Math.max(0, sheet.clockPly - 1);
  el.plyPlus.disabled = ply >= sheet.clockPly + 1;
  const p = photos.find((x) => x.n === sheet.n);
  if (p) {
    el.sheetMeta.textContent = `clock at ply ${p.ply}`
      + (ply !== p.ply ? `, confirming at ply ${ply}` : '')
      + ` · ${ply % 2 === 0 ? 'White' : 'Black'} to move · ${p.model.replace('claude-', '')}`;
  }
}

el.plyMinus.addEventListener('click', () => { sheet.ply -= 1; showPly(); showFlags(null); });
el.plyPlus.addEventListener('click', () => { sheet.ply += 1; showPly(); showFlags(null); });
el.sheetRotate.addEventListener('click', () => sheet.editor && sheet.editor.rotate());
el.sheetUndo.addEventListener('click', () => sheet.editor && sheet.editor.reset());

el.sheetConfirm.addEventListener('click', async () => {
  if (!sheet || !sheet.editor) return;
  el.sheetConfirm.disabled = true;
  try {
    const entry = await api(`/api/games/${game.game_id}/photos/${sheet.n}/confirm`, {
      method: 'POST',
      body: { rows: sheet.editor.rows, home: sheet.editor.home, ply: sheet.ply },
    });
    upsertPhoto(entry);
    const fixes = entry.confirmed.corrections.length;
    toast(`Board confirmed${fixes ? `, ${fixes} square${fixes === 1 ? '' : 's'} fixed` : ''}`);
    closeSheet();
  } catch (err) {
    if (err.status === 422) {
      showFlags((err.detail && err.detail.flags) || ['impossible']);
    } else {
      toast(`Could not confirm (${err.message})`);
    }
  } finally {
    el.sheetConfirm.disabled = false;
  }
});

el.sheetRetry.addEventListener('click', async () => {
  if (!sheet) return;
  if (sheet.failedFile) {
    const { failedFile, failedShot } = sheet;
    closeSheet();
    uploadPhoto(failedFile, failedShot);
    return;
  }
  const n = sheet.n;
  showReading(null, 'Reading the board again…');
  try {
    const entry = await api(`/api/games/${game.game_id}/photos/${n}/read`, { method: 'POST' });
    upsertPhoto(entry);
    if (sheet && sheet.n === n) showSheetFor(entry);
  } catch (err) {
    await refreshPhotos();
    toast(`Could not read again (${err.message})`);
  }
});

el.sheetManual.addEventListener('click', () => {
  if (!sheet) return;
  sheet.manual = true;
  const p = photos.find((x) => x.n === sheet.n);
  if (p) showSheetFor(p);
});

el.sheetRetake.addEventListener('click', () => {
  if (!sheet) return;
  const p = sheet.n != null ? photos.find((x) => x.n === sheet.n) : null;
  const confirmed = p && p.status === 'confirmed';
  const stamp = p ? { ply: p.ply, turn: p.turn, white_ms: p.white_ms, black_ms: p.black_ms }
    : (sheet.failedShot ? sheet.failedShot.stamp : null);
  closeSheet();
  // A retake replaces an unconfirmed photo; a confirmed one stays and the new
  // photo is simply another photo at the same ply.
  takePhoto(p && !confirmed ? p.n : null, stamp);
});

el.sheetLater.addEventListener('click', closeSheet);

el.sheetResume.addEventListener('click', () => {
  closeSheet();
  resumeClock();
});

function refreshSheetButtons() {
  el.sheetResume.hidden = sm.state !== 'paused';
}

/* ---------------- render loop ---------------- */

function render(now) {
  el.timeWhite.textContent = formatClock(sm.remaining('white', now));
  el.timeBlack.textContent = formatClock(sm.remaining('black', now));

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

  if (sm.state === 'flagged' && !flagSent) {
    flagSent = true;
    beep();
    send({
      type: 'clock.flag',
      ply: wireSeq(sm.seq),
      side: sm.flaggedSide,
      white_ms: sm.remaining('white', now),
      black_ms: sm.remaining('black', now),
      t: Date.now(),
    });
    save();
    gameIsOver(sm.flaggedSide === 'white' ? '0-1' : '1-0');
  }

  el.plyLabel.textContent = `ply ${wireSeq(sm.seq)}`;
  const shots = live().map((p) => p.ply);
  el.cameraBtn.classList.toggle('hint', photoHint({
    state: sm.state,
    ply: wireSeq(sm.seq),
    lastPhotoPly: shots.length ? Math.max(...shots) : null,
    threshold: hintPlies,
    whiteMs: sm.remaining('white', now),
    blackMs: sm.remaining('black', now),
  }));
  el.pauseResumeBtn.textContent = sm.state === 'paused' ? 'Resume' : 'Pause';
  el.pauseResumeBtn.disabled = sm.state !== 'running' && sm.state !== 'paused';
  el.stopBtn.disabled = sm.state === 'ready' || sm.state === 'stopped' || sm.state === 'flagged';
  if (sheet) refreshSheetButtons();
}

function loop(ts) {
  sm.tick(ts);
  if (game) render(ts);
  requestAnimationFrame(loop);
}

/* ---------------- boot ---------------- */

// A reload, or iOS bringing the page back after the camera: catch up on reads.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && game) refreshPhotos();
});

async function boot() {
  keepAwake();
  const wantsNew = new URLSearchParams(location.search).has('new');
  if (wantsNew) history.replaceState(null, '', '/clock');
  if (wantsNew || !(await resumeSavedGame())) await showSetup();
  requestAnimationFrame(loop);
}

boot();
