// Review page, photos section: settings, every photo with its position, and
// the same tap-to-fix editor as the clock page for boards skipped during play.
// Protocol: docs/PROTOCOL.md §4–5.

import { BoardEditor, homeSelect } from './board.js';
import { api, toast } from './ws.js';

const gameId = new URLSearchParams(location.search).get('game');
const $ = (id) => document.getElementById(id);
const card = $('photos-card');
const list = $('ph-list');
const form = $('ph-settings');

let meta = null;
let photos = [];
let editing = null;   // {n, editor, ply, clockPly}

function txt(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderHeader() {
  $('ph-players').textContent = `${meta.white_name} (${meta.white_rating}) – ${meta.black_name} (${meta.black_rating})`;
  const live = photos.filter((p) => p.status !== 'replaced');
  const open = live.filter((p) => p.status === 'read' || p.status === 'error').length;
  const cost = live.reduce((s, p) => s + ((p.read && p.read.cost) || 0), 0);
  $('ph-summary').textContent = `${live.length} photo${live.length === 1 ? '' : 's'}, `
    + `${live.length - open} confirmed${open ? `, ${open} to confirm` : ''}`
    + ` · reader ${meta.reader_model.replace('claude-', '')} · $${cost.toFixed(2)} spent on reads`;
}

function fillSettings(models) {
  for (const k of ['white_name', 'black_name', 'white_rating', 'black_rating']) {
    form.elements[k].value = meta[k];
  }
  const sel = form.elements.reader_model;
  sel.innerHTML = '';
  for (const m of models) {
    const o = document.createElement('option');
    o.value = m;
    o.textContent = m.replace('claude-', '');
    sel.appendChild(o);
  }
  sel.value = meta.reader_model;
}

$('ph-settings-btn').addEventListener('click', (e) => {
  form.hidden = !form.hidden;
  e.currentTarget.setAttribute('aria-expanded', String(!form.hidden));
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {
    white_name: form.elements.white_name.value.trim() || 'White',
    black_name: form.elements.black_name.value.trim() || 'Black',
    white_rating: Number(form.elements.white_rating.value),
    black_rating: Number(form.elements.black_rating.value),
    reader_model: form.elements.reader_model.value,
  };
  try {
    Object.assign(meta, await api(`/api/games/${gameId}/settings`, { method: 'PATCH', body }));
    renderHeader();
    toast('Settings saved');
    window.dispatchEvent(new Event('boardcam:changed'));
  } catch (err) {
    toast(`Not saved: ${err.message}`);
  }
});

const STATUS = {
  reading: 'reading', read: 'to confirm', error: 'read failed', confirmed: 'confirmed', replaced: 'replaced',
};

function renderList() {
  list.innerHTML = '';
  const shown = photos.filter((p) => p.status !== 'replaced');
  if (!shown.length) {
    list.appendChild(txt('p', 'muted', 'No photos in this game.'));
    return;
  }
  for (const p of shown) {
    const item = txt('div', 'ph-item');
    const a = document.createElement('a');
    a.href = `/games/${gameId}/photos/${p.n}`;
    a.target = '_blank';
    a.rel = 'noopener';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = `Photo ${p.n}`;
    img.src = a.href;
    a.appendChild(img);
    item.appendChild(a);

    const head = txt('div', 'ph-head');
    const ply = p.status === 'confirmed' ? p.confirmed.ply : p.ply;
    head.append(txt('strong', null, `#${p.n} ply ${ply}`));
    head.append(txt('span', `ph-status ${p.status === 'read' || p.status === 'error' ? 'open' : ''}`, STATUS[p.status]));
    item.appendChild(head);

    if (p.status === 'confirmed') {
      item.appendChild(txt('div', 'ph-fen', p.confirmed.fen));
      const fixes = p.confirmed.corrections.length;
      item.appendChild(txt('p', 'muted num', `${fixes} square${fixes === 1 ? '' : 's'} fixed`
        + (p.confirmed.home_changed ? ", White's side changed" : '')));
    } else if (p.status === 'error') {
      item.appendChild(txt('p', 'ph-flags', `${p.error.reason}: ${p.error.detail || ''}`));
    } else if (p.position && !p.position.legal) {
      item.appendChild(txt('p', 'ph-flags', `impossible: ${p.position.flags.join(', ')}`));
    }

    if (p.status !== 'reading') {
      const row = txt('div', 'row');
      const b = txt('button', 'btn', p.status === 'confirmed' ? 'Edit' : 'Confirm');
      b.type = 'button';
      b.addEventListener('click', () => openEditor(p, item));
      row.appendChild(b);
      if (p.status === 'error' && p.error.retry) {
        const r = txt('button', 'btn', 'Read again');
        r.type = 'button';
        r.addEventListener('click', () => reread(p.n, r));
        row.appendChild(r);
      }
      item.appendChild(row);
    }
    list.appendChild(item);
  }
}

function openEditor(p, item) {
  document.querySelectorAll('.ph-editor').forEach((x) => x.remove());
  const box = txt('div', 'ph-editor card stack');
  const flags = txt('p', 'ph-flags');
  flags.hidden = true;
  const boardEl = document.createElement('div');
  const base = p.status === 'confirmed' ? p.confirmed : (p.read || { rows: null, home: 'bottom' });
  const editor = new BoardEditor(boardEl, { rows: base.rows, home: base.home, editable: true,
    onChange: () => { flags.hidden = true; } });
  editing = { n: p.n, editor, ply: p.status === 'confirmed' ? p.confirmed.ply : p.ply, clockPly: p.ply };

  const plyLabel = txt('span', 'num');
  const minus = txt('button', 'btn', '−');
  const plus = txt('button', 'btn', '+');
  [minus, plus].forEach((b) => { b.type = 'button'; });
  const showPly = () => {
    plyLabel.textContent = `ply ${editing.ply} · ${editing.ply % 2 === 0 ? 'White' : 'Black'} to move`;
    minus.disabled = editing.ply <= Math.max(0, editing.clockPly - 1);
    plus.disabled = editing.ply >= editing.clockPly + 1;
  };
  minus.addEventListener('click', () => { editing.ply -= 1; showPly(); });
  plus.addEventListener('click', () => { editing.ply += 1; showPly(); });
  showPly();

  const flip = txt('button', 'btn', 'Flip view');
  flip.type = 'button';
  flip.title = 'Turn the diagram over; the position does not change';
  flip.addEventListener('click', () => editor.flip());
  const undo = txt('button', 'btn', 'Undo fixes');
  undo.type = 'button';
  undo.addEventListener('click', () => editor.reset());
  const save = txt('button', 'btn btn-primary', 'Confirm board');
  save.type = 'button';
  const cancel = txt('button', 'btn', 'Close');
  cancel.type = 'button';
  cancel.addEventListener('click', () => box.remove());

  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const entry = await api(`/api/games/${gameId}/photos/${p.n}/confirm`, {
        method: 'POST', body: { rows: editor.rows, home: editor.home, ply: editing.ply },
      });
      photos = photos.map((x) => (x.n === entry.n ? entry : x));
      renderHeader();
      renderList();
      toast('Board confirmed');
      window.dispatchEvent(new Event('boardcam:changed'));
    } catch (err) {
      flags.hidden = false;
      flags.textContent = err.status === 422 && err.detail && err.detail.flags
        ? `Impossible position: ${err.detail.flags.join(', ')}.`
        : `Not saved: ${err.message}`;
    } finally {
      save.disabled = false;
    }
  });

  const tools = txt('div', 'row');
  tools.append(minus, plyLabel, plus, txt('span', 'spacer'), flip, undo);
  const homeRow = txt('div', 'row');
  homeRow.append(homeSelect(editor));
  const actions = txt('div', 'row');
  actions.append(save, cancel);
  box.append(txt('h3', null, `Photo ${p.n}`), flags, boardEl, tools, homeRow, actions);
  item.after(box);
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function reread(n, btn) {
  btn.disabled = true;
  btn.textContent = 'Reading…';
  try {
    const entry = await api(`/api/games/${gameId}/photos/${n}/read`, { method: 'POST' });
    photos = photos.map((x) => (x.n === entry.n ? entry : x));
  } catch (err) {
    toast(`Read failed: ${err.message}`);
  }
  renderHeader();
  renderList();
}

async function load() {
  if (!gameId) return;
  try {
    const [detail, settings] = await Promise.all([
      api(`/api/games/${gameId}`),
      api('/api/settings'),
    ]);
    meta = detail;
    photos = detail.positions.photos;
    fillSettings(settings.reader_models);
  } catch {
    return;   // review.js reports a game that cannot be loaded
  }
  card.hidden = false;
  renderHeader();
  renderList();
}

load();
