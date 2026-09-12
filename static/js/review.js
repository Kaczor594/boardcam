// Review and correction page (Phase 5). Protocol: docs/PROTOCOL.md §8.
import { api, modal, toast } from '/static/js/ws.js';

const gameId = new URLSearchParams(location.search).get('game');

const statusMsg = document.getElementById('status-msg');
const content = document.getElementById('content');

const playersEl = document.getElementById('players');
const metaLine = document.getElementById('meta-line');
const copyPgnBtn = document.getElementById('copy-pgn-btn');
const lichessBtn = document.getElementById('lichess-btn');
const lichessLink = document.getElementById('lichess-link');
const deleteBtn = document.getElementById('delete-btn');
const resultChips = document.getElementById('result-chips');
const verifyToggle = document.getElementById('verify-toggle');

const moveRows = document.getElementById('move-rows');
const detailEmpty = document.getElementById('detail-empty');
const detailBody = document.getElementById('detail-body');
const detailTitle = document.getElementById('detail-title');
const detailFlags = document.getElementById('detail-flags');
const imgRawBefore = document.getElementById('img-raw-before');
const imgRawAfter = document.getElementById('img-raw-after');
const imgRectBefore = document.getElementById('img-rect-before');
const imgRectAfter = document.getElementById('img-rect-after');
const candidatesList = document.getElementById('candidates-list');
const frameKv = document.getElementById('frame-kv');
const sanInput = document.getElementById('san-input');
const correctBtn = document.getElementById('correct-btn');
const unpinBtn = document.getElementById('unpin-btn');
const correctionStatus = document.getElementById('correction-status');
const pgnText = document.getElementById('pgn-text');

const RESULTS = ['1-0', '0-1', '1/2-1/2', '*'];

const state = { data: null, selected: null, busy: false };

function fail(msg) {
  statusMsg.textContent = msg;
  content.hidden = true;
}

function setBusy(busy) {
  state.busy = busy;
  sanInput.disabled = busy;
  correctBtn.disabled = busy;
  unpinBtn.disabled = busy;
  correctionStatus.hidden = !busy;
  for (const chip of resultChips.querySelectorAll('.chip')) chip.disabled = busy;
  verifyToggle.disabled = busy;
  deleteBtn.disabled = busy;
}

function when(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/* ---------------- header ---------------- */

function renderHeader() {
  const d = state.data;
  playersEl.textContent = `${d.white_name} — ${d.black_name}`;
  const flagged = d.analysis ? d.analysis.plies.filter((p) => p.flags.length > 0).length : 0;
  const parts = [
    when(d.created_at),
    d.result || '*',
    d.status,
    `${flagged} flagged`,
    d.game_id,
  ];
  metaLine.textContent = parts.join(' · ');

  if (d.lichess_url) {
    lichessLink.href = d.lichess_url;
    lichessLink.hidden = false;
    lichessBtn.hidden = true;
  } else {
    lichessLink.hidden = true;
    lichessBtn.hidden = false;
  }
}

function renderResultChips() {
  resultChips.replaceChildren();
  for (const value of RESULTS) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = value;
    chip.setAttribute('aria-pressed', String(state.data.result === value));
    chip.addEventListener('click', () => setResult(value));
    resultChips.appendChild(chip);
  }
}

function renderVerify() {
  const verified = !!state.data.labels.verified;
  verifyToggle.setAttribute('aria-pressed', String(verified));
  verifyToggle.textContent = verified ? 'Verified as correct' : 'Confirm this game is correct';
}

function renderPgn() {
  pgnText.textContent = state.data.pgn || '';
}

async function setResult(value) {
  if (state.busy) return;
  setBusy(true);
  try {
    const resp = await api(`/api/games/${gameId}/result`, { method: 'POST', body: { result: value } });
    state.data.result = resp.result;
    state.data.pgn = resp.pgn;
    renderHeader();
    renderResultChips();
    renderPgn();
    toast('Result updated.');
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}

copyPgnBtn.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(state.data.pgn || '');
    toast('PGN copied.');
  } catch {
    toast('Could not copy PGN.');
  }
});

lichessBtn.addEventListener('click', async () => {
  lichessBtn.disabled = true;
  try {
    const resp = await api(`/api/games/${gameId}/lichess`, { method: 'POST' });
    if (resp.url) {
      state.data.lichess_url = resp.url;
      renderHeader();
      window.open(resp.url, '_blank', 'noopener');
    } else {
      toast('lichess declined the import.');
    }
  } catch (err) {
    toast(err.message);
  } finally {
    lichessBtn.disabled = false;
  }
});

deleteBtn.addEventListener('click', async () => {
  const ok = await modal({
    title: 'Delete this game?',
    body: 'The game directory, frames and event log will be removed. This cannot be undone.',
    row: true,
    choices: [
      { label: 'Cancel', value: false },
      { label: 'Delete', value: true, cls: 'btn-danger' },
    ],
  });
  if (!ok) return;
  try {
    await api(`/api/games/${gameId}`, { method: 'DELETE' });
    location.href = '/games';
  } catch (err) {
    toast(`Could not delete: ${err.message}`);
  }
});

verifyToggle.addEventListener('click', async () => {
  if (state.busy) return;
  const next = !state.data.labels.verified;
  setBusy(true);
  try {
    const resp = await api(`/api/games/${gameId}/verify`, { method: 'POST', body: { verified: next } });
    state.data.labels.verified = resp.verified;
    renderVerify();
    toast(resp.verified ? 'Marked as training truth.' : 'Verification cleared.');
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
});

/* ---------------- move list ---------------- */

function isPinned(index) {
  return state.data.labels.corrections.some((c) => c.ply === index);
}

function badgeFor(ply) {
  if (isPinned(ply.index)) return { cls: 'badge-pinned', label: 'pinned' };
  if (ply.flags.includes('unexplained')) return { cls: 'badge-error', label: 'unexplained' };
  if (ply.flags.includes('promotion_unknown')) return { cls: 'badge-warn', label: 'promotion?' };
  if (ply.flags.includes('low_margin')) {
    const label = ply.margin === null ? '∞' : ply.margin.toFixed(1);
    return { cls: 'badge-warn', label };
  }
  return null;
}

function plySpan(ply) {
  const span = document.createElement('span');
  span.className = 'ply';
  span.dataset.index = String(ply.index);
  span.tabIndex = 0;
  span.textContent = ply.san;
  const badge = badgeFor(ply);
  if (badge) {
    const b = document.createElement('span');
    b.className = `badge ${badge.cls}`;
    b.textContent = badge.label;
    span.appendChild(b);
  }
  span.addEventListener('click', () => selectPly(ply.index));
  return span;
}

function renderMoveList() {
  moveRows.replaceChildren();
  const plies = state.data.analysis.plies;
  for (let i = 0; i < plies.length; i += 2) {
    const tr = document.createElement('tr');
    const num = document.createElement('td');
    num.className = 'movenum num';
    num.textContent = `${Math.floor(i / 2) + 1}.`;
    tr.appendChild(num);

    const whiteTd = document.createElement('td');
    whiteTd.appendChild(plySpan(plies[i]));
    tr.appendChild(whiteTd);

    const blackTd = document.createElement('td');
    if (plies[i + 1]) blackTd.appendChild(plySpan(plies[i + 1]));
    tr.appendChild(blackTd);

    moveRows.appendChild(tr);
  }
  highlightSelection();
}

function highlightSelection() {
  for (const el of moveRows.querySelectorAll('.ply')) {
    el.classList.toggle('selected', Number(el.dataset.index) === state.selected);
  }
}

function flashChanged(indices) {
  for (const idx of indices || []) {
    const el = moveRows.querySelector(`.ply[data-index="${idx}"]`);
    if (!el) continue;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  }
}

/* ---------------- detail pane ---------------- */

function beforeSeqFor(index) {
  const plies = state.data.analysis.plies;
  return index === 0 ? 0 : plies[index - 1].seq;
}

function setImage(img, url) {
  img.hidden = false;
  img.onerror = () => {
    img.hidden = true;
    let fallback = img.nextElementSibling;
    if (!fallback || !fallback.classList.contains('img-fallback')) {
      fallback = document.createElement('p');
      fallback.className = 'img-fallback';
      fallback.textContent = 'unavailable';
      img.after(fallback);
    }
  };
  const existingFallback = img.nextElementSibling;
  if (existingFallback && existingFallback.classList.contains('img-fallback')) existingFallback.remove();
  img.src = url;
}

function rectUrl(seq, prevSeq, squares) {
  const params = new URLSearchParams({ k: '0', prev: String(prevSeq), squares });
  return `/api/games/${gameId}/rect/${seq}?${params}`;
}

function frameRecordFor(seq) {
  return (state.data.analysis.frames || []).find((f) => f.seq === seq) || null;
}

function renderFrameKv(frame) {
  frameKv.replaceChildren();
  if (!frame) {
    const kv = document.createElement('span');
    kv.className = 'kv';
    kv.textContent = 'no frame record';
    frameKv.appendChild(kv);
    return;
  }
  for (const [key, value] of Object.entries(frame)) {
    const kv = document.createElement('span');
    kv.className = 'kv';
    let text = value;
    if (typeof value === 'number') text = Number.isInteger(value) ? value : value.toFixed(3);
    else if (Array.isArray(value)) text = value.length ? value.join(',') : '—';
    else if (value === null || value === undefined) text = '—';
    kv.textContent = `${key}: ${text}`;
    frameKv.appendChild(kv);
  }
}

function renderCandidates(ply) {
  candidatesList.replaceChildren();
  const cands = ply.candidates || [];
  if (!cands.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'No alternatives were scored at this frame.';
    candidatesList.appendChild(li);
    return;
  }
  const top = cands[0];
  for (const c of cands) {
    const li = document.createElement('li');
    li.className = 'candidate-row' + (c === top ? ' top' : '');

    const san = document.createElement('span');
    san.textContent = c.san;
    li.appendChild(san);

    const score = document.createElement('span');
    score.textContent = `${c.score.toFixed(1)} nats`;
    li.appendChild(score);

    const delta = document.createElement('span');
    delta.className = 'delta';
    const below = top.score - c.score;
    delta.textContent = below > 0 ? `-${below.toFixed(1)} vs top` : 'top';
    li.appendChild(delta);

    li.addEventListener('click', () => { sanInput.value = c.san; sanInput.focus(); });
    candidatesList.appendChild(li);
  }
}

function selectPly(index) {
  state.selected = index;
  highlightSelection();
  renderDetail();
  const el = moveRows.querySelector(`.ply[data-index="${index}"]`);
  if (el) el.scrollIntoView({ block: 'nearest' });
}

function renderDetail() {
  const plies = state.data.analysis.plies;
  if (state.selected === null || !plies[state.selected]) {
    detailEmpty.hidden = false;
    detailBody.hidden = true;
    return;
  }
  detailEmpty.hidden = true;
  detailBody.hidden = false;

  const ply = plies[state.selected];
  const moveNo = Math.floor(ply.index / 2) + 1;
  const dots = ply.index % 2 === 0 ? '.' : '...';
  detailTitle.textContent = `${moveNo}${dots} ${ply.san}`;

  detailFlags.replaceChildren();
  const badge = badgeFor(ply);
  if (badge) {
    const b = document.createElement('span');
    b.className = `badge ${badge.cls}`;
    b.textContent = badge.label;
    detailFlags.appendChild(b);
  }
  const marginText = document.createElement('span');
  marginText.className = 'muted';
  marginText.style.fontSize = 'var(--text-sm)';
  marginText.textContent = `margin: ${ply.margin === null ? '∞' : ply.margin.toFixed(1) + ' nats'}`;
  detailFlags.appendChild(marginText);

  const beforeSeq = beforeSeqFor(ply.index);
  const afterSeq = ply.seq;
  const squares = ply.uci ? `${ply.uci.slice(0, 2)},${ply.uci.slice(2, 4)}` : '';

  setImage(imgRawBefore, `/games/${gameId}/frames/${beforeSeq}/0`);
  setImage(imgRawAfter, `/games/${gameId}/frames/${afterSeq}/0`);
  setImage(imgRectBefore, rectUrl(beforeSeq, afterSeq, squares));
  setImage(imgRectAfter, rectUrl(afterSeq, beforeSeq, squares));

  renderCandidates(ply);
  renderFrameKv(frameRecordFor(afterSeq));

  unpinBtn.hidden = !isPinned(ply.index);
  sanInput.value = '';
}

/* ---------------- corrections ---------------- */

correctBtn.addEventListener('click', async () => {
  if (state.busy || state.selected === null) return;
  const san = sanInput.value.trim();
  if (!san) { toast('Enter a SAN move first.'); return; }
  setBusy(true);
  try {
    const resp = await api(`/api/games/${gameId}/corrections`, {
      method: 'POST',
      body: { ply: state.selected, san },
    });
    applyReviewResponse(resp, { keepSelection: true, flash: resp.changed });
    toast('Correction applied.');
  } catch (err) {
    // 400 = illegal move; leave the input as-is either way so it can be edited.
    toast(err.message);
  } finally {
    setBusy(false);
  }
});

unpinBtn.addEventListener('click', async () => {
  if (state.busy || state.selected === null) return;
  setBusy(true);
  try {
    const resp = await api(`/api/games/${gameId}/corrections/${state.selected}`, { method: 'DELETE' });
    applyReviewResponse(resp, { keepSelection: true, flash: resp.changed });
    toast('Unpinned.');
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
});

/* ---------------- keyboard nav ---------------- */

document.addEventListener('keydown', (e) => {
  if (state.busy || !state.data || !state.data.analysis) return;
  const active = document.activeElement;
  if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) return;

  const plies = state.data.analysis.plies;
  if (!plies.length) return;

  let next = null;
  if (e.key === 'ArrowDown' || e.key === 'j') next = (state.selected === null ? 0 : state.selected + 1);
  else if (e.key === 'ArrowUp' || e.key === 'k') next = (state.selected === null ? 0 : state.selected - 1);
  if (next === null) return;

  next = Math.max(0, Math.min(plies.length - 1, next));
  e.preventDefault();
  selectPly(next);
});

/* ---------------- load / re-render ---------------- */

function applyReviewResponse(data, { keepSelection = false, flash = [] } = {}) {
  const prevSelected = state.selected;
  state.data = data;
  renderHeader();
  renderResultChips();
  renderVerify();
  renderPgn();
  renderMoveList();

  const plies = data.analysis.plies;
  if (keepSelection && prevSelected !== null && plies[prevSelected]) {
    selectPly(prevSelected);
  } else if (plies.length) {
    selectPly(0);
  } else {
    state.selected = null;
    renderDetail();
  }
  flashChanged(flash);
}

async function load() {
  if (!gameId) {
    statusMsg.replaceChildren();
    statusMsg.append('No game specified. ');
    const a = document.createElement('a');
    a.href = '/games';
    a.textContent = 'Back to games';
    statusMsg.appendChild(a);
    statusMsg.append('.');
    return;
  }

  let data;
  try {
    data = await api(`/api/games/${gameId}/review`);
  } catch (err) {
    fail(`Could not load this game: ${err.message}`);
    return;
  }

  if (data.pending) {
    const why = data.status === 'finished'
      ? 'The game finished but no analysis is available yet — the tracker may have failed.'
      : 'This game is still being played. Analysis appears once it finishes.';
    statusMsg.textContent = `${why} (status: ${data.status}${data.error ? `, error: ${data.error}` : ''})`;
    content.hidden = true;
    return;
  }

  statusMsg.textContent = '';
  content.hidden = false;
  applyReviewResponse(data, { keepSelection: false });
}

load();
