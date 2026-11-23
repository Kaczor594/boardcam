// Label page: Fable drafts a board, Isaac confirms it from the photo, and taps
// the four corners of the playing area. Protocol: docs/PROTOCOL.md §10.
//
// Corners are entered by hand and never pre-filled by the grid finder, so its
// evaluation is not anchored on its own answer. Taps are in the page's
// downscaled upright photo; they are saved in full-resolution pixels, in board
// order: the outer corners of a8, h8, h1, a1.

import { BoardEditor, homeSelect, HOME_SIDES } from './board.js';
import { api, NGROK_HEADER, toast } from './ws.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const EMPTY = Array(8).fill('........');
const BOARD_NAMES = ['a8', 'h8', 'h1', 'a1'];

let setName = params.get('set') || 'green-mat';
let all = [];            // photo list of the set
let shown = [];          // after the filter
let cur = null;          // the open photo's API entry
let editor = null;
let bitmap = null;       // the displayed photo
let pts = [];            // tapped corners, view pixels [[x, y], …]
let turn = 0;            // extra quarter turns of the corner names, on top of the home side
let drag = -1;

// ---------------------------------------------------------------- corners

/** Clockwise in the image (y down), starting nearest the top-left — engine/gridfind._order_cw. */
function orderCw(q) {
  const cx = q.reduce((s, p) => s + p[0], 0) / q.length;
  const cy = q.reduce((s, p) => s + p[1], 0) / q.length;
  const o = q.slice().sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
  let start = 0;
  o.forEach((p, i) => { if (p[0] + p[1] < o[start][0] + o[start][1]) start = i; });
  return o.slice(start).concat(o.slice(0, start));
}

/** Indices into [TL, TR, BR, BL] giving a8, h8, h1, a1 — engine/labels.board_corner_order. */
function boardOrder(home) {
  const base = { bottom: [0, 1, 2, 3], top: [2, 3, 0, 1], left: [1, 2, 3, 0], right: [3, 0, 1, 2] }[home] || [0, 1, 2, 3];
  return base.map((k) => (k + turn) % 4);
}

/** The tapped corners in board order (view pixels), or null until there are four. */
function boardCorners() {
  if (pts.length !== 4) return null;
  const cw = orderCw(pts);
  return boardOrder(editor.home).map((k) => cw[k]);
}

/** Board-order corners from the stored truth back to view pixels (turn = 0 by construction). */
function loadCorners(corners) {
  pts = [];
  turn = 0;
  if (!corners) return;
  const s = viewScale();
  const view = corners.map(([x, y]) => [x / s, y / s]);
  // Find the name turn that reproduces the stored order from the clockwise order.
  pts = view;
  for (let t = 0; t < 4; t += 1) {
    turn = t;
    const got = boardCorners();
    if (got.every((p, i) => p === view[i])) return;
  }
  turn = 0;
}

function viewScale() {
  return cur.size[0] / bitmap.width;          // full-res px per view px
}

// ---------------------------------------------------------------- drawing

const canvas = $('photo');
const ctx = canvas.getContext('2d');
const loupe = $('loupe');
const lctx = loupe.getContext('2d');

function draw() {
  if (!bitmap) return;
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  ctx.drawImage(bitmap, 0, 0);
  const lw = Math.max(2, bitmap.width / 500);
  const bc = boardCorners();
  if (bc) {
    ctx.strokeStyle = '#B96F3E';
    ctx.lineWidth = lw;
    // the 8x8 grid through the four corners, to judge the fit square by square
    const H = homography(bc);
    for (let k = 0; k <= 8; k += 1) {
      line(H, [k, 0], [k, 8]);
      line(H, [0, k], [8, k]);
    }
  }
  const named = bc ? BOARD_NAMES : null;
  pts.forEach((p, i) => {
    ctx.beginPath();
    ctx.arc(p[0], p[1], lw * 5, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.fill();
    ctx.lineWidth = lw;
    ctx.strokeStyle = '#0E0D09';
    ctx.stroke();
  });
  if (named) {
    ctx.font = `600 ${Math.round(lw * 12)}px sans-serif`;
    bc.forEach((p, i) => {
      ctx.lineWidth = lw * 3;
      ctx.strokeStyle = 'white';
      ctx.strokeText(named[i], p[0] + lw * 7, p[1] - lw * 7);
      ctx.fillStyle = named[i] === 'a1' ? '#3E5A32' : '#0E0D09';
      ctx.fillText(named[i], p[0] + lw * 7, p[1] - lw * 7);
    });
  }
  const note = $('corner-line');
  if (pts.length === 4) {
    note.textContent = 'Four corners. Check that a1 is White\'s left-hand corner.';
    note.className = 'ok';
  } else {
    note.textContent = `${pts.length} of 4 corners`;
    note.className = '';
  }
}

function line(H, a, b) {
  const p = apply(H, a); const q = apply(H, b);
  ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
}

/** Homography from board coords (a8 = 0,0 … h1 = 8,8) to the four view points. */
function homography(dst) {
  const src = [[0, 0], [8, 0], [8, 8], [0, 8]];
  const A = []; const b = [];
  for (let i = 0; i < 4; i += 1) {
    const [x, y] = src[i]; const [u, v] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
    A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
  }
  const h = solve(A, b);
  return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1];
}

function apply(H, [x, y]) {
  const w = H[6] * x + H[7] * y + H[8];
  return [(H[0] * x + H[1] * y + H[2]) / w, (H[3] * x + H[4] * y + H[5]) / w];
}

function solve(A, b) {             // Gaussian elimination, 8x8
  const n = b.length; const M = A.map((r, i) => r.concat([b[i]]));
  for (let c = 0; c < n; c += 1) {
    let p = c;
    for (let r = c + 1; r < n; r += 1) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    [M[c], M[p]] = [M[p], M[c]];
    for (let r = 0; r < n; r += 1) {
      if (r === c) continue;
      const f = M[r][c] / M[c][c];
      for (let k = c; k <= n; k += 1) M[r][k] -= f * M[c][k];
    }
  }
  return M.map((r, i) => r[n] / r[i]);
}

function showLoupe(x, y) {
  const z = 4; const r = loupe.width / 2;
  loupe.style.display = 'block';
  lctx.clearRect(0, 0, loupe.width, loupe.height);
  lctx.save();
  lctx.beginPath(); lctx.arc(r, r, r, 0, Math.PI * 2); lctx.clip();
  lctx.drawImage(canvas, x - r / z, y - r / z, (2 * r) / z, (2 * r) / z, 0, 0, 2 * r, 2 * r);
  lctx.strokeStyle = '#B96F3E'; lctx.lineWidth = 1;
  lctx.beginPath(); lctx.moveTo(r, 0); lctx.lineTo(r, 2 * r); lctx.moveTo(0, r); lctx.lineTo(2 * r, r); lctx.stroke();
  lctx.restore();
  // keep the loupe away from the finger
  const rect = canvas.getBoundingClientRect();
  const left = (x / canvas.width) * rect.width < rect.width / 2;
  loupe.style.left = left ? '' : '8px';
  loupe.style.right = left ? '8px' : '';
}

function viewPoint(ev) {
  const rect = canvas.getBoundingClientRect();
  // object-fit: contain — the drawn photo may be letterboxed inside the element
  const s = Math.min(rect.width / canvas.width, rect.height / canvas.height);
  const ox = (rect.width - canvas.width * s) / 2; const oy = (rect.height - canvas.height * s) / 2;
  return [(ev.clientX - rect.left - ox) / s, (ev.clientY - rect.top - oy) / s];
}

canvas.addEventListener('pointerdown', (ev) => {
  if (!bitmap) return;
  const p = viewPoint(ev);
  const near = bitmap.width / 40;
  drag = pts.findIndex((q) => Math.hypot(q[0] - p[0], q[1] - p[1]) < near);
  if (drag < 0 && pts.length < 4) {
    pts.push(p);
    drag = pts.length - 1;
  }
  canvas.setPointerCapture(ev.pointerId);
  draw();
  showLoupe(p[0], p[1]);
});
canvas.addEventListener('pointermove', (ev) => {
  if (!bitmap) return;
  const p = viewPoint(ev);
  if (drag >= 0) { pts[drag] = p; draw(); }
  if (drag >= 0 || ev.pointerType === 'mouse') showLoupe(p[0], p[1]);
});
const endDrag = () => { drag = -1; loupe.style.display = 'none'; };
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);
canvas.addEventListener('pointerleave', () => { if (drag < 0) loupe.style.display = 'none'; });

$('turn').addEventListener('click', () => { turn = (turn + 1) % 4; draw(); });
$('clear-corners').addEventListener('click', () => { pts = []; turn = 0; draw(); });

// ---------------------------------------------------------------- photos

function status(text, warn = false) {
  const s = $('status');
  s.textContent = text;
  s.className = warn ? 'warn' : '';
}

function mark(p) {
  const bits = [];
  if (p.excluded) bits.push('excluded');
  else bits.push(p.done ? 'board ✓' : 'board –', p.corners ? 'corners ✓' : 'corners –');
  if (p.source && p.source.startsWith('game:') && !p.checked) bits.push('unchecked');
  return `${p.name} · ${p.split} · ${bits.join(' · ')}`;
}

function applyFilter() {
  const f = $('filter').value;
  shown = all.filter((p) => {
    if (f === 'board') return !p.done && !p.excluded;
    if (f === 'corners') return !p.corners && !p.excluded;
    if (f === 'unchecked') return (p.source || '').startsWith('game:') && !p.checked;
    return true;
  });
  const pick = $('pick');
  pick.innerHTML = '';
  for (const p of shown) {
    const o = document.createElement('option');
    o.value = p.name;
    o.textContent = mark(p);
    pick.appendChild(o);
  }
}

async function loadSet() {
  const res = await api(`/api/labels/${setName}`);
  all = res.photos;
  applyFilter();
}

async function open(name) {
  if (!name) { status('Nothing to show with this filter.'); return; }
  status('');
  cur = await api(`/api/labels/${setName}/${encodeURIComponent(name)}`);
  $('pick').value = name;
  const i = shown.findIndex((p) => p.name === name);
  $('pos').textContent = `${i + 1} / ${shown.length}`;
  history.replaceState(null, '', `?set=${setName}&photo=${encodeURIComponent(name)}`);
  $('meta').textContent = `${name} · session ${cur.session || '?'} · ${cur.split} · ${cur.source || ''}`;

  editor = new BoardEditor($('board'), {
    rows: cur.rows || EMPTY, home: HOME_SIDES.includes(cur.home) ? cur.home : 'bottom',
    editable: true, onChange: () => draw(),
  });
  const slot = $('home-slot');
  slot.innerHTML = '';
  slot.appendChild(homeSelect(editor));
  $('done').checked = !!cur.done;
  $('exc').checked = !!cur.excluded;
  $('why').value = cur.excluded || '';

  const resp = await fetch(`/labels/${setName}/photos/${encodeURIComponent(name)}`, { headers: NGROK_HEADER });
  bitmap = await createImageBitmap(await resp.blob());
  loadCorners(cur.corners);
  draw();
}

function step(d) {
  const i = shown.findIndex((p) => p.name === (cur && cur.name));
  const j = Math.min(shown.length - 1, Math.max(0, i + d));
  if (shown[j]) open(shown[j].name);
}

async function save() {
  const excluded = $('exc').checked ? ($('why').value.trim() || 'excluded') : null;
  const bc = boardCorners();
  if (pts.length && !bc) { status('Tap all four corners, or clear them.', true); return false; }
  const s = viewScale();
  const body = {
    rows: editor.rows, home: editor.home, done: $('done').checked, excluded,
    corners: bc ? bc.map(([x, y]) => [x * s, y * s]) : null,
  };
  try {
    cur = { ...cur, ...(await api(`/api/labels/${setName}/${encodeURIComponent(cur.name)}`, { method: 'PUT', body })) };
  } catch (err) {
    status(err.message, true);
    return false;
  }
  const p = all.find((q) => q.name === cur.name);
  Object.assign(p, { done: cur.done, excluded: cur.excluded, corners: !!cur.corners, checked: true });
  const o = [...$('pick').options].find((x) => x.value === cur.name);
  if (o) o.textContent = mark(p);
  toast('Saved');
  return true;
}

$('save').addEventListener('click', save);
$('save-next').addEventListener('click', async () => { if (await save()) step(1); });
$('prev').addEventListener('click', () => step(-1));
$('next').addEventListener('click', () => step(1));
$('pick').addEventListener('change', (e) => open(e.target.value));
$('filter').addEventListener('change', () => { applyFilter(); open(shown[0] && shown[0].name); });
$('flip').addEventListener('click', () => editor.flip());
$('undo').addEventListener('click', () => editor.reset());
$('empty').addEventListener('click', () => editor.load(EMPTY, editor.home));
$('draft').addEventListener('click', async () => {
  status('Reading… (up to a few minutes for a diagonal photo)');
  $('draft').disabled = true;
  try {
    const r = await api(`/api/labels/${setName}/${encodeURIComponent(cur.name)}/draft`, { method: 'POST' });
    editor.load(r.rows, r.home);
    $('home-slot').innerHTML = '';
    $('home-slot').appendChild(homeSelect(editor));
    status(`Draft by ${r.model} ($${(r.cost || 0).toFixed(2)}). Check every square against the photo.`);
    draw();
  } catch (err) {
    status(err.message, true);
  } finally {
    $('draft').disabled = false;
  }
});

$('set').addEventListener('change', async (e) => {
  setName = e.target.value;
  await loadSet();
  open(shown[0] && shown[0].name);
});

$('up-go').addEventListener('click', async () => {
  const files = $('up-files').files;
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('split', $('up-split').value);
  status(`Uploading ${files.length}…`);
  try {
    const r = await api(`/api/labels/${setName}/upload`, { method: 'POST', body: fd });
    status(`Added ${r.added.length}.`);
    $('up-files').value = '';
    await loadSet();
    open(r.added[0]);
  } catch (err) {
    status(err.message, true);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, select, textarea')) return;
  if (e.key === 'ArrowRight') step(1);
  else if (e.key === 'ArrowLeft') step(-1);
});

(async () => {
  const { sets } = await api('/api/labels');
  const sel = $('set');
  for (const s of sets) {
    const o = document.createElement('option');
    o.value = s.set;
    o.textContent = `${s.set} (${s.labelled}/${s.photos} labelled, ${s.corners} corners)`;
    sel.appendChild(o);
  }
  if (!sets.some((s) => s.set === setName) && sets.length) setName = sets[0].set;
  sel.value = setName;
  await loadSet();
  const want = params.get('photo');
  open(shown.some((p) => p.name === want) ? want : shown[0] && shown[0].name);
})();
