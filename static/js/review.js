// Review page: the game between the photos. Protocol: docs/PROTOCOL.md §8.
//
// One payload (GET /api/games/{id}/review) holds the timeline: one entry per
// ply along the most likely line between the photos. The page steps through
// it on a board, draws the likely next moves and Stockfish's best moves as
// arrows, lets the player accept a move (a pin, which re-solves that gap),
// draws the evaluation graph, and exports the PGN.

import { BoardEditor, drawArrows, rowsFromFen } from './board.js';
import { api, modal, toast } from './ws.js';

const gameId = new URLSearchParams(location.search).get('game');
const $ = (id) => document.getElementById(id);
const RESULTS = ['1-0', '0-1', '1/2-1/2', '*'];
const CLAMP = 6;   // pawns, for the graph

const state = { data: null, i: 0, engine: new Map(), poll: null, busy: false, follow: null };
const board = new BoardEditor($('board'), { rows: rowsFromFen('8/8/8/8/8/8/8/8 w - - 0 1'), home: 'bottom' });

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function when(ts) {
  return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const pct = (p) => (p == null ? '–' : p >= 0.995 ? '>99 %' : `${Math.round(p * 100)} %`);
const moveName = (uci, san) => san || `${uci.slice(0, 2)}–${uci.slice(2, 4)}${uci.slice(4)}`;
const moveNo = (ply) => `${Math.ceil(ply / 2)}${ply % 2 ? '.' : '…'}`;

function fmtEval(ev) {
  if (!ev) return '–';
  if (ev.mate != null) return ev.mate === 0 ? 'mate' : `M${ev.mate > 0 ? '' : '−'}${Math.abs(ev.mate)}`;
  const p = ev.cp / 100;
  return `${p > 0 ? '+' : p < 0 ? '−' : ''}${Math.abs(p).toFixed(1)}`;
}

/* ---------------- loading ---------------- */

async function load({ quiet = false } = {}) {
  try {
    state.data = await api(`/api/games/${gameId}/review`);
  } catch (err) {
    if (!quiet) $('status-msg').textContent = err.status === 404 ? 'No such game.' : `Could not load the game: ${err.message}`;
    return;
  }
  $('status-msg').textContent = '';
  $('content').hidden = false;
  const tl = state.data.timeline;
  if (state.i >= tl.length) state.i = tl.length - 1;
  // After an accept, step onto the accepted move as soon as the timeline
  // contains it (at once if it was already the top line, else after the re-solve).
  if (state.follow) {
    const k = tl.findIndex((e) => e.ply === state.follow.ply && e.uci === state.follow.uci);
    if (k >= 0) { state.i = k; state.follow = null; }
    else if (!state.data.solving && !state.data.stale) state.follow = null;
  }
  renderAll();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.poll);
  const d = state.data;
  if (d.stale && !d.solving) { solve(); return; }   // e.g. the server restarted mid-solve
  if (d.solving || d.stale) state.poll = setTimeout(() => load({ quiet: true }), 2000);
}

async function solve() {
  try {
    await api(`/api/games/${gameId}/solve`, { method: 'POST' });
  } catch (err) {
    toast(`Could not start the solver: ${err.message}`);
  }
  state.data.solving = true;
  renderSolveLine();
  schedulePoll();
}

/* ---------------- header ---------------- */

function renderHeader() {
  const d = state.data;
  $('players').textContent = `${d.white_name} (${d.white_rating}) – ${d.black_name} (${d.black_rating})`;
  const photos = d.fens.length;
  $('meta-line').textContent = [when(d.created_at), d.result || '*', `${d.plies} plies clocked`,
    `${photos} confirmed photo${photos === 1 ? '' : 's'}`, d.game_id].join(' · ');
  $('pgn-download').href = `/games/${gameId}/pgn`;
  $('lichess-link').hidden = !d.lichess_url;
  $('lichess-btn').hidden = !!d.lichess_url;
  if (d.lichess_url) $('lichess-link').href = d.lichess_url;
  $('pgn-text').textContent = d.pgn || '';

  const chips = $('result-chips');
  chips.replaceChildren();
  for (const value of RESULTS) {
    const chip = el('button', 'chip', value);
    chip.type = 'button';
    chip.setAttribute('aria-pressed', String(d.result === value));
    chip.addEventListener('click', () => setResult(value));
    chips.appendChild(chip);
  }
  renderSolveLine();
}

function renderSolveLine() {
  const d = state.data;
  const line = $('solve-line');
  line.classList.remove('warn');
  const bad = d.gaps.filter((g) => g.status !== 'found');
  if (d.solving || d.stale) {
    line.textContent = `Working out the moves between the photos… (${d.gaps.length} of ${d.fens.length} gaps done)`;
  } else if (!d.fens.length) {
    line.textContent = 'No confirmed photos yet: confirm a board below to see the moves.';
  } else if (bad.length) {
    line.classList.add('warn');
    line.textContent = bad.map((g) => (g.status === 'none'
      ? (g.pins
        ? `No legal sequence of ${g.n_requested} plies with your accepted moves reaches photo ${g.to.photo}: undo one of them.`
        : `No legal sequence of ${g.n_requested} plies reaches photo ${g.to.photo}: check that board or its ply.`)
      : g.status === 'order' ? `Photo ${g.to.photo} is before the end of the line leading to it.`
        : `The search for the ${g.n_requested} plies before photo ${g.to.photo} ran out of time.`)).join(' ');
  } else {
    const after = d.plies - d.last_photo_ply;
    line.textContent = `${d.gaps.length} gap${d.gaps.length === 1 ? '' : 's'} solved.`
      + (after > 0 ? ` The last ${after} plies were played after the last photo and are not recorded.` : '');
  }
}

/* ---------------- board and arrows ---------------- */

function renderBoard() {
  const tl = state.data.timeline;
  const e = tl[state.i];
  board.load(rowsFromFen(e.fen), 'bottom');
  $('ply-label').textContent = e.ply === 0 ? 'start' : `${moveNo(e.ply)} ${moveName(e.uci || '', e.san)}`;
  const parts = [`ply ${e.ply}`];
  if (e.photo != null) parts.push(`photo ${e.photo}, confirmed`);
  else if (e.kind === 'pinned') parts.push('accepted by you');
  else if (e.kind === 'inferred') parts.push(`inferred, ${pct(e.p)} of likely lines`);
  if (e.eval) parts.push(`eval ${fmtEval(e.eval)}`);
  $('ply-state').textContent = parts.join(' · ');
  $('nav-first').disabled = $('nav-prev').disabled = state.i === 0;
  $('nav-last').disabled = $('nav-next').disabled = state.i === tl.length - 1;
  renderNext();
}

/** The move out of the current position: its marginals live on the next entry. */
function nextEntry() {
  const tl = state.data.timeline;
  const n = tl[state.i + 1];
  return n && !n.break ? n : null;
}

function renderNext() {
  const d = state.data;
  const e = d.timeline[state.i];
  const next = nextEntry();
  const list = $('derived-list');
  list.replaceChildren();
  const arrows = [];
  const note = $('next-note');
  note.textContent = '';
  if (!next) {
    note.textContent = state.i === d.timeline.length - 1 ? '(after the last photo)' : '(gap not solved)';
  } else {
    const pin = d.pins.find((p) => p.ply === next.ply);
    if (pin) note.textContent = `— ${moveName(pin.uci, pin.san)} accepted`;
    for (const m of next.alts.slice(0, 5)) {
      arrows.push({ uci: m.uci, kind: 'derived', p: m.p });
      const li = el('li');
      li.append(el('span', 'mv', moveName(m.uci, m.san)));
      const bar = el('span', 'bar');
      const fill = el('span');
      fill.style.width = `${Math.round(m.p * 100)}%`;
      bar.appendChild(fill);
      li.append(bar);
      const right = el('span', 'row');
      right.style.gap = 'var(--space-2)';
      right.append(el('span', 'pct', pct(m.p)));
      if (pin && pin.uci === m.uci) {
        const b = el('button', 'btn', 'Undo');
        b.type = 'button';
        b.addEventListener('click', () => unpin(next.ply));
        right.append(b);
      } else if (!pin) {
        const b = el('button', 'btn', 'Accept');
        b.type = 'button';
        b.title = 'This move was played: keep it and work out the rest of the gap again';
        b.addEventListener('click', () => pinMove(next.ply, m.uci));
        right.append(b);
      }
      li.append(right);
      list.appendChild(li);
    }
  }
  renderEngine(e, arrows);
}

async function renderEngine(e, derivedArrows) {
  const list = $('engine-list');
  const key = e.fen;
  const draw = (moves) => {
    if (state.data.timeline[state.i].fen !== key) return;   // moved on meanwhile
    list.replaceChildren();
    for (const m of moves) {
      const li = el('li');
      li.append(el('span', 'mv', m.san), el('span', 'muted', ''), el('span', 'pct', fmtEval(m)));
      list.appendChild(li);
    }
    if (!moves.length) list.appendChild(el('li', 'muted', 'No moves: the game is over here.'));
    drawArrows($('board'), [...moves.map((m) => ({ uci: m.uci, kind: 'engine' })), ...derivedArrows]);
  };
  drawArrows($('board'), derivedArrows);
  if (state.engine.has(key)) { draw(state.engine.get(key)); return; }
  list.replaceChildren(el('li', 'muted', 'Stockfish is thinking…'));
  try {
    const res = await api(`/api/games/${gameId}/engine/${e.ply}`);
    state.engine.set(key, res.moves);
    draw(res.moves);
  } catch {
    if (state.data.timeline[state.i].fen === key) list.replaceChildren(el('li', 'muted', 'Stockfish is not available.'));
  }
}

function go(i) {
  const tl = state.data.timeline;
  state.i = Math.max(0, Math.min(tl.length - 1, i));
  renderBoard();
  renderMoves();
  renderGraph();
}

$('nav-first').addEventListener('click', () => go(0));
$('nav-prev').addEventListener('click', () => go(state.i - 1));
$('nav-next').addEventListener('click', () => go(state.i + 1));
$('nav-last').addEventListener('click', () => go(Infinity));
document.addEventListener('keydown', (ev) => {
  if (!state.data || ev.target.closest('input, select, textarea')) return;
  if (ev.key === 'ArrowLeft') { go(state.i - 1); ev.preventDefault(); }
  if (ev.key === 'ArrowRight') { go(state.i + 1); ev.preventDefault(); }
});

/* ---------------- pins ---------------- */

async function pinMove(ply, uci) {
  if (state.busy) return;
  state.busy = true;
  try {
    await api(`/api/games/${gameId}/pins`, { method: 'POST', body: { ply, uci } });
    state.follow = { ply, uci };
    await solve();
    await load({ quiet: true });
    if (state.follow) toast('Move accepted; working out the rest of the gap');
  } catch (err) {
    toast(`Not accepted: ${err.message}`);
  } finally {
    state.busy = false;
  }
}

async function unpin(ply) {
  if (state.busy) return;
  state.busy = true;
  try {
    await api(`/api/games/${gameId}/pins/${ply}`, { method: 'DELETE' });
    toast('Accepted move undone');
    await solve();
    await load({ quiet: true });
  } catch (err) {
    toast(`Not undone: ${err.message}`);
  } finally {
    state.busy = false;
  }
}

/* ---------------- move list ---------------- */

function renderMoves() {
  const tl = state.data.timeline;
  const body = $('move-rows');
  body.replaceChildren();
  let tr = null;
  const btn = (i) => {
    const e = tl[i];
    const b = el('button', `ply ${e.certain ? 'certain' : ''} ${e.kind === 'pinned' ? 'pinned' : ''} ${i === state.i ? 'selected' : ''}`,
      moveName(e.uci, e.san));
    b.type = 'button';
    if (e.photo != null) b.append(el('span', 'tag', `◆${e.photo}`));
    b.addEventListener('click', () => go(i));
    return b;
  };
  for (let i = 1; i < tl.length; i += 1) {
    const e = tl[i];
    if (e.break) {
      const note = el('tr', 'gapnote');
      const td = el('td', null, `Unsolved gap · photo ${e.photo} at ply ${e.ply}`);
      td.colSpan = 3;
      const jump = el('button', 'ply certain', '→');
      jump.type = 'button';
      jump.addEventListener('click', () => go(i));
      td.append(' ', jump);
      note.appendChild(td);
      body.appendChild(note);
      tr = null;
      continue;
    }
    if (e.ply % 2 === 1 || !tr) {
      tr = el('tr');
      tr.append(el('td', 'movenum', `${Math.ceil(e.ply / 2)}.`), el('td'), el('td'));
      body.appendChild(tr);
      if (e.ply % 2 === 0) tr.children[1].textContent = '…';
    }
    tr.children[e.ply % 2 === 1 ? 1 : 2].appendChild(btn(i));
  }
  const sel = body.querySelector('.selected');
  // Keep the selected move visible inside the list's own scroll box only;
  // scrollIntoView would also scroll the page, which on a phone jumps away
  // from the board on every step or accept.
  const box = body.closest('.movelist-scroll');
  if (sel && box) {
    const s = sel.getBoundingClientRect(), b = box.getBoundingClientRect();
    if (s.top < b.top) box.scrollTop -= b.top - s.top;
    else if (s.bottom > b.bottom) box.scrollTop += s.bottom - b.bottom;
  }
}

/* ---------------- evaluation graph ---------------- */

const SVGNS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};
const pawns = (ev) => (ev.mate != null ? (ev.mate === 0 ? (ev.cp > 0 ? CLAMP : -CLAMP) : Math.sign(ev.mate) * CLAMP)
  : Math.max(-CLAMP, Math.min(CLAMP, ev.cp / 100)));

function renderGraph() {
  const host = $('graph');
  host.replaceChildren();
  const tl = state.data.timeline.filter((e) => e.eval);
  if (tl.length < 2) {
    host.appendChild(el('p', 'muted', 'The graph appears once a gap is solved.'));
    return;
  }
  const W = Math.max(320, host.clientWidth || 800);
  const H = 220;
  const m = { l: 34, r: 10, t: 10, b: 22 };
  const maxPly = Math.max(1, tl[tl.length - 1].ply);
  const x = (ply) => m.l + (ply / maxPly) * (W - m.l - m.r);
  const y = (v) => m.t + ((CLAMP - v) / (2 * CLAMP)) * (H - m.t - m.b);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Evaluation by ply, pawns from White\'s side' });

  for (const v of [-4, -2, 0, 2, 4]) {
    svg.appendChild(svgEl('line', { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v),
      stroke: v === 0 ? 'var(--line-strong)' : 'var(--line-1)', 'stroke-width': 1 }));
    const t = svgEl('text', { x: m.l - 6, y: y(v) + 3, 'text-anchor': 'end', 'font-size': 10,
      'font-family': 'var(--font-mono)', fill: 'var(--fg-3)' });
    t.textContent = v > 0 ? `+${v}` : String(v);
    svg.appendChild(t);
  }
  const step = maxPly > 60 ? 20 : 10;
  for (let p = 0; p <= maxPly; p += step) {
    const t = svgEl('text', { x: x(p), y: H - 6, 'text-anchor': 'middle', 'font-size': 10,
      'font-family': 'var(--font-mono)', fill: 'var(--fg-3)' });
    t.textContent = String(p);
    svg.appendChild(t);
  }

  // segments: solid between two certain positions, dashed otherwise; none across an unsolved gap
  for (let k = 1; k < tl.length; k += 1) {
    const a = tl[k - 1];
    const b = tl[k];
    if (b.break || b.ply !== a.ply + 1) continue;
    const solid = a.certain && b.certain;
    svg.appendChild(svgEl('line', { x1: x(a.ply), y1: y(pawns(a.eval)), x2: x(b.ply), y2: y(pawns(b.eval)),
      stroke: 'var(--fg-1)', 'stroke-width': 2, 'stroke-linecap': 'round',
      'stroke-dasharray': solid ? 'none' : '3 4' }));
  }
  for (const e of tl) {
    if (e.photo == null) continue;
    svg.appendChild(svgEl('circle', { cx: x(e.ply), cy: y(pawns(e.eval)), r: 4.5,
      fill: 'var(--fg-1)', stroke: 'var(--bg-raised)', 'stroke-width': 2 }));
  }
  const cur = state.data.timeline[state.i];
  svg.appendChild(svgEl('line', { x1: x(cur.ply), x2: x(cur.ply), y1: m.t, y2: H - m.b,
    stroke: 'var(--arrow-derived)', 'stroke-width': 1.5 }));

  const hover = svgEl('line', { y1: m.t, y2: H - m.b, stroke: 'var(--line-strong)', 'stroke-width': 1,
    visibility: 'hidden' });
  svg.appendChild(hover);
  const tip = el('div', 'tip');
  tip.hidden = true;

  const nearest = (clientX) => {
    const r = svg.getBoundingClientRect();
    const px = ((clientX - r.left) / r.width) * W;
    const ply = Math.round(((px - m.l) / (W - m.l - m.r)) * maxPly);
    let best = 0;
    for (let k = 1; k < tl.length; k += 1) if (Math.abs(tl[k].ply - ply) < Math.abs(tl[best].ply - ply)) best = k;
    return tl[best];
  };
  svg.addEventListener('pointermove', (ev) => {
    const e = nearest(ev.clientX);
    hover.setAttribute('x1', x(e.ply));
    hover.setAttribute('x2', x(e.ply));
    hover.setAttribute('visibility', 'visible');
    tip.hidden = false;
    tip.textContent = `ply ${e.ply}${e.san ? ` · ${moveNo(e.ply)} ${e.san}` : ''} · ${fmtEval(e.eval)}`
      + (e.photo != null ? ` · photo ${e.photo}` : e.certain ? '' : ' · inferred');
    const r = svg.getBoundingClientRect();
    const left = (x(e.ply) / W) * r.width;
    tip.style.left = `${Math.min(r.width - tip.offsetWidth - 4, Math.max(0, left + 8))}px`;
    tip.style.top = '4px';
  });
  svg.addEventListener('pointerleave', () => { hover.setAttribute('visibility', 'hidden'); tip.hidden = true; });
  svg.addEventListener('click', (ev) => {
    const e = nearest(ev.clientX);
    go(state.data.timeline.indexOf(e));
  });
  host.append(svg, tip);
}

window.addEventListener('resize', () => { if (state.data) renderGraph(); });

/* ---------------- actions ---------------- */

async function setResult(value) {
  try {
    const resp = await api(`/api/games/${gameId}/result`, { method: 'POST', body: { result: value } });
    state.data.result = resp.result;
    state.data.pgn = resp.pgn;
    renderHeader();
    toast('Result updated');
  } catch (err) {
    toast(err.message);
  }
}

async function copy(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied`);
  } catch {
    toast('The browser refused the clipboard');
  }
}

$('copy-pgn-btn').addEventListener('click', () => copy(state.data.pgn || '', 'PGN'));
$('copy-fens-btn').addEventListener('click', () => copy(
  state.data.fens.map((f) => `photo ${f.photo}, ply ${f.ply}: ${f.fen}`).join('\n'), 'FENs'));

$('lichess-btn').addEventListener('click', async () => {
  const b = $('lichess-btn');
  b.disabled = true;
  try {
    const { url } = await api(`/api/games/${gameId}/lichess`, { method: 'POST' });
    if (url) {
      state.data.lichess_url = url;
      renderHeader();
      window.open(url, '_blank', 'noopener');
    } else {
      toast('lichess did not accept the import; try again later');
    }
  } catch (err) {
    toast(err.message);
  } finally {
    b.disabled = false;
  }
});

$('delete-btn').addEventListener('click', async () => {
  const ok = await modal({
    title: 'Delete this game?',
    body: 'The photos, positions, clock log and inferred moves will be removed. This cannot be undone.',
    row: true,
    choices: [{ label: 'Cancel', value: false }, { label: 'Delete', value: true, cls: 'btn-danger' }],
  });
  if (!ok) return;
  try {
    await api(`/api/games/${gameId}`, { method: 'DELETE' });
    location.href = '/games';
  } catch (err) {
    toast(err.message);
  }
});

// photos.js fires this after a board is confirmed or the settings change
window.addEventListener('boardcam:changed', () => load({ quiet: true }));

function renderAll() {
  renderHeader();
  renderBoard();
  renderMoves();
  renderGraph();
}

if (!gameId) {
  $('status-msg').textContent = 'No game given.';
} else {
  load();
}
