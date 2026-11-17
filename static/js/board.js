// Board view and tap-to-fix editor. No network: callers own fetching and saving.
//
// The reader reports a board in the *photo frame* (row 0 farthest from the
// camera, column 0 leftmost) plus the side of the photo White's home rank is
// on. The editor keeps exactly that pair — rows + home — because it is the
// training label Phase 5 wants, and draws it as a diagram with White at the
// bottom. "Rotate" changes only the home side; tapping a square changes only
// the photo-frame cell under it. Mirrors engine/position.py:square_of.

export const HOME_SIDES = ['bottom', 'right', 'top', 'left'];   // rotate cycles in this order

/** Board square {file 0..7, rank 0..7} of photo cell (r, c) for a White home side. */
export function squareOf(r, c, home) {
  switch (home) {
    case 'bottom': return { file: c, rank: 7 - r };
    case 'top': return { file: 7 - c, rank: r };
    case 'left': return { file: r, rank: c };
    case 'right': return { file: 7 - r, rank: 7 - c };
    default: throw new Error(`bad home side ${home}`);
  }
}

/** Photo cell {r, c} under board square (file, rank). */
export function cellOf(file, rank, home) {
  for (let r = 0; r < 8; r += 1) {
    for (let c = 0; c < 8; c += 1) {
      const sq = squareOf(r, c, home);
      if (sq.file === file && sq.rank === rank) return { r, c };
    }
  }
  throw new Error('unreachable');
}

// The solid glyph for both colours; colour comes from CSS (paper fill with an
// ink outline for White). Fonts disagree on which of the outline/solid code
// points are hollow, so relying on them made Black's king look like White's.
// U+FE0E keeps iOS from drawing the pawn as an emoji.
const SOLID = { k: '\u265A', q: '\u265B', r: '\u265C', b: '\u265D', n: '\u265E', p: '\u265F' };
export const PIECE_NAMES = {
  K: 'white king', Q: 'white queen', R: 'white rook', B: 'white bishop', N: 'white knight', P: 'white pawn',
  k: 'black king', q: 'black queen', r: 'black rook', b: 'black bishop', n: 'black knight', p: 'black pawn',
  '.': 'empty',
};
const PALETTE = ['.', 'K', 'Q', 'R', 'B', 'N', 'P', 'k', 'q', 'r', 'b', 'n', 'p'];
const FILES = 'abcdefgh';

export function glyph(ch) {
  return ch === '.' ? '' : `${SOLID[ch.toLowerCase()]}\uFE0E`;
}

/**
 * A board diagram, optionally editable.
 *
 *   const ed = new BoardEditor(container, { rows, home, editable: true, onChange });
 *   ed.rotate(); ed.rows; ed.home; ed.changed  // cells that differ from the read
 */
export class BoardEditor {
  constructor(container, { rows, home, editable = false, onChange = null } = {}) {
    this.container = container;
    this.editable = editable;
    this.onChange = onChange;
    this.selected = null;          // {file, rank}
    this.load(rows, home);
  }

  /** Start over from a read (also the baseline that `changed` compares with). */
  load(rows, home) {
    this.original = (rows || Array(8).fill('........')).slice();
    this.rows = this.original.slice();
    this.originalHome = HOME_SIDES.includes(home) ? home : 'bottom';
    this.home = this.originalHome;
    this.selected = null;
    this.render();
  }

  get changed() {
    const out = [];
    for (let r = 0; r < 8; r += 1) {
      for (let c = 0; c < 8; c += 1) {
        if (this.rows[r][c] !== this.original[r][c]) out.push({ r, c });
      }
    }
    return out;
  }

  rotate() {
    this.home = HOME_SIDES[(HOME_SIDES.indexOf(this.home) + 1) % 4];
    this.selected = null;
    this.render();
    if (this.onChange) this.onChange(this);
  }

  reset() {
    this.load(this.original, this.originalHome);
    if (this.onChange) this.onChange(this);
  }

  pieceAt(file, rank) {
    const { r, c } = cellOf(file, rank, this.home);
    return this.rows[r][c];
  }

  setPiece(file, rank, ch) {
    const { r, c } = cellOf(file, rank, this.home);
    const row = this.rows[r];
    this.rows[r] = row.slice(0, c) + ch + row.slice(c + 1);
    this.render();
    if (this.onChange) this.onChange(this);
  }

  render() {
    const root = this.container;
    root.innerHTML = '';
    root.classList.add('bd');

    const grid = document.createElement('div');
    grid.className = 'bd-grid';
    grid.setAttribute('role', 'grid');
    grid.setAttribute('aria-label', 'Board, White at the bottom');
    const changed = new Set(this.changed.map(({ r, c }) => {
      const sq = squareOf(r, c, this.home);
      return `${sq.file}${sq.rank}`;
    }));

    for (let rank = 7; rank >= 0; rank -= 1) {
      for (let file = 0; file < 8; file += 1) {
        const ch = this.pieceAt(file, rank);
        const cell = document.createElement(this.editable ? 'button' : 'div');
        if (this.editable) cell.type = 'button';
        cell.className = `bd-sq ${(file + rank) % 2 ? 'bd-light' : 'bd-dark'}`;
        if (ch !== '.') cell.classList.add(ch === ch.toUpperCase() ? 'bd-white' : 'bd-black');
        if (changed.has(`${file}${rank}`)) cell.classList.add('bd-changed');
        if (this.selected && this.selected.file === file && this.selected.rank === rank) {
          cell.classList.add('bd-selected');
        }
        cell.textContent = glyph(ch);
        cell.setAttribute('aria-label', `${FILES[file]}${rank + 1} ${PIECE_NAMES[ch]}`);
        if (file === 0) cell.dataset.rank = String(rank + 1);
        if (rank === 0) cell.dataset.file = FILES[file];
        if (this.editable) {
          cell.addEventListener('click', () => {
            const same = this.selected && this.selected.file === file && this.selected.rank === rank;
            this.selected = same ? null : { file, rank };
            this.render();
          });
        }
        grid.appendChild(cell);
      }
    }
    root.appendChild(grid);

    if (this.editable && this.selected) {
      const { file, rank } = this.selected;
      const pal = document.createElement('div');
      pal.className = 'bd-palette';
      const label = document.createElement('span');
      label.className = 'bd-palette-label num';
      label.textContent = `${FILES[file]}${rank + 1}`;
      pal.appendChild(label);
      const current = this.pieceAt(file, rank);
      for (const ch of PALETTE) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = `bd-pick ${ch === '.' ? 'bd-pick-empty' : (ch === ch.toUpperCase() ? 'bd-white' : 'bd-black')}`;
        b.textContent = ch === '.' ? '×' : glyph(ch);
        b.setAttribute('aria-label', PIECE_NAMES[ch]);
        b.setAttribute('aria-pressed', String(ch === current));
        b.addEventListener('click', () => {
          this.selected = null;
          this.setPiece(file, rank, ch);
        });
        pal.appendChild(b);
      }
      root.appendChild(pal);
    }
  }
}

/** Shared styles, injected once so every page that imports board.js gets them. */
const CSS = `
.bd { width: 100%; max-width: 420px; margin: 0 auto; }
.bd-grid { display: grid; grid-template-columns: repeat(8, 1fr); aspect-ratio: 1;
  border: 1px solid var(--line-strong); user-select: none; -webkit-user-select: none; }
.bd-sq { position: relative; display: flex; align-items: center; justify-content: center;
  aspect-ratio: 1; padding: 0; margin: 0; border: 0; border-radius: 0; min-height: 0;
  font: 400 clamp(1.3rem, 8.5vw, 2.4rem)/1 "Segoe UI Symbol", "Apple Symbols", "DejaVu Sans", serif;
  color: var(--ink); cursor: default; }
button.bd-sq { cursor: pointer; }
.bd-white { color: var(--paper-00); -webkit-text-stroke: 1.2px var(--ink); paint-order: stroke fill; }
.bd-black { color: var(--ink); }
.bd-light { background: var(--paper-20); }
.bd-dark { background: var(--stone-40); }
.bd-sq.bd-changed { box-shadow: inset 0 0 0 2px var(--brand); }
.bd-sq.bd-selected { box-shadow: inset 0 0 0 3px var(--ink); }
.bd-sq::before, .bd-sq::after { -webkit-text-stroke: 0; }
.bd-sq[data-rank]::before { content: attr(data-rank); position: absolute; left: 2px; top: 1px;
  font: 500 9px/1 var(--font-mono); color: var(--fg-3); }
.bd-sq[data-file]::after { content: attr(data-file); position: absolute; right: 2px; bottom: 1px;
  font: 500 9px/1 var(--font-mono); color: var(--fg-3); }
.bd-palette { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin-top: var(--space-2); }
.bd-palette-label { font-family: var(--font-mono); font-size: var(--text-sm); color: var(--fg-2);
  min-width: 2.2em; }
.bd-pick { width: 40px; height: 40px; padding: 0; border: 1px solid var(--line-1);
  border-radius: var(--radius-sm); background: var(--bg-raised); color: var(--ink);
  font: 400 26px/1 "Apple Symbols", "Segoe UI Symbol", serif; cursor: pointer; }
.bd-pick[aria-pressed="true"] { border-color: var(--ink); box-shadow: inset 0 0 0 1px var(--ink); }
.bd-pick-empty { font: 500 18px/1 var(--font-sans); color: var(--fg-3); }
`;

if (typeof document !== 'undefined' && !document.getElementById('bd-css')) {
  const style = document.createElement('style');
  style.id = 'bd-css';
  style.textContent = CSS;
  document.head.appendChild(style);
}
