/* Minimal QR encoder: byte mode, error correction level M, versions 1–10.
   Enough for a URL like https://name.ngrok-free.app/clock?room=K4WQ (≈50
   bytes, version 3). Written out rather than pulled from a CDN because the
   phone may be on a captive venue network with no outbound internet. */

// Data codewords available in byte mode at EC level M, versions 1..10.
const DATA_CODEWORDS_M = [null, 16, 28, 44, 64, 86, 108, 124, 154, 182, 216];
// EC codewords per block, and the block layout [count1, count2] for level M.
const EC_PER_BLOCK_M = [null, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26];
const BLOCKS_M = [null, [1, 0], [1, 0], [1, 0], [2, 0], [2, 0], [4, 0], [4, 0], [2, 2], [3, 2], [4, 1]];
// Centres of the alignment patterns, by version.
const ALIGN_POS = [
  null, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
  [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50],
];

/* ---- GF(256) arithmetic, primitive polynomial 0x11d ---- */
const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
(function initTables() {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
})();

function gfMul(a, b) {
  if (a === 0 || b === 0) return 0;
  return EXP[LOG[a] + LOG[b]];
}

/** Generator polynomial of degree `degree`. */
function generatorPoly(degree) {
  let poly = [1];
  for (let i = 0; i < degree; i++) {
    const next = new Array(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= poly[j];
      next[j + 1] ^= gfMul(poly[j], EXP[i]);
    }
    poly = next;
  }
  return poly;
}

/** Reed-Solomon remainder of `data` for `ecLen` check codewords. */
function ecCodewords(data, ecLen) {
  const gen = generatorPoly(ecLen);
  const rem = new Array(ecLen).fill(0);
  for (const byte of data) {
    const factor = byte ^ rem[0];
    rem.shift();
    rem.push(0);
    for (let i = 0; i < ecLen; i++) rem[i] ^= gfMul(gen[i + 1], factor);
  }
  return rem;
}

/* ---- format and version information ---- */

const FORMAT_MASK = 0b101010000010010;

/** 15-bit format info for EC level M (bits 00) and mask pattern `mask`. */
function formatBits(mask) {
  const data = (0b00 << 3) | mask;         // EC level M is 0b00
  let rem = data;
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ (((rem >> 9) & 1) * 0b10100110111);
  return (((data << 10) | rem) ^ FORMAT_MASK) & 0x7fff;
}

/** 18-bit version info, used from version 7 up. */
function versionBits(version) {
  let rem = version;
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ (((rem >> 11) & 1) * 0b1111100100101);
  return (version << 12) | rem;
}

/* ---- bit stream ---- */

class BitBuffer {
  constructor() { this.bits = []; }
  put(value, length) {
    for (let i = length - 1; i >= 0; i--) this.bits.push((value >> i) & 1);
  }
  get length() { return this.bits.length; }
}

/** Smallest version 1..10 whose byte-mode capacity holds `byteLen`. */
function chooseVersion(byteLen) {
  for (let v = 1; v <= 10; v++) {
    const countBits = v <= 9 ? 8 : 16;
    const needed = 4 + countBits + byteLen * 8;
    if (needed <= DATA_CODEWORDS_M[v] * 8) return v;
  }
  return null;
}

function encodeData(bytes, version) {
  const buf = new BitBuffer();
  buf.put(0b0100, 4);                                  // byte mode
  buf.put(bytes.length, version <= 9 ? 8 : 16);
  for (const b of bytes) buf.put(b, 8);

  const capacityBits = DATA_CODEWORDS_M[version] * 8;
  buf.put(0, Math.min(4, capacityBits - buf.length));   // terminator
  while (buf.length % 8 !== 0) buf.bits.push(0);

  const codewords = [];
  for (let i = 0; i < buf.length; i += 8) {
    let byte = 0;
    for (let j = 0; j < 8; j++) byte = (byte << 1) | buf.bits[i + j];
    codewords.push(byte);
  }
  // Pad alternately with 0xEC and 0x11 until the block is full.
  const pads = [0xec, 0x11];
  for (let i = 0; codewords.length < DATA_CODEWORDS_M[version]; i++) {
    codewords.push(pads[i % 2]);
  }
  return codewords;
}

/** Split into blocks, append EC, then interleave as the spec requires. */
function interleave(codewords, version) {
  const [n1, n2] = BLOCKS_M[version];
  const totalBlocks = n1 + n2;
  const ecLen = EC_PER_BLOCK_M[version];
  const shortLen = Math.floor(DATA_CODEWORDS_M[version] / totalBlocks);

  const dataBlocks = [];
  const ecBlocks = [];
  let offset = 0;
  for (let b = 0; b < totalBlocks; b++) {
    const len = b < n1 ? shortLen : shortLen + 1;
    const block = codewords.slice(offset, offset + len);
    offset += len;
    dataBlocks.push(block);
    ecBlocks.push(ecCodewords(block, ecLen));
  }

  const out = [];
  const maxData = Math.max(...dataBlocks.map((b) => b.length));
  for (let i = 0; i < maxData; i++) {
    for (const block of dataBlocks) if (i < block.length) out.push(block[i]);
  }
  for (let i = 0; i < ecLen; i++) {
    for (const block of ecBlocks) out.push(block[i]);
  }
  return out;
}

/* ---- matrix ---- */

function emptyMatrix(size) {
  return Array.from({ length: size }, () => new Array(size).fill(null));
}

function placeFinder(m, row, col) {
  for (let r = -1; r <= 7; r++) {
    for (let c = -1; c <= 7; c++) {
      const rr = row + r;
      const cc = col + c;
      if (rr < 0 || rr >= m.length || cc < 0 || cc >= m.length) continue;
      const inRing = (r >= 0 && r <= 6 && (c === 0 || c === 6))
                  || (c >= 0 && c <= 6 && (r === 0 || r === 6));
      const inCore = r >= 2 && r <= 4 && c >= 2 && c <= 4;
      m[rr][cc] = inRing || inCore ? 1 : 0;
    }
  }
}

function placeAlignment(m, version) {
  const centres = ALIGN_POS[version];
  const size = m.length;
  for (const r of centres) {
    for (const c of centres) {
      // Skip the three corners already occupied by finder patterns.
      if ((r === 6 && c === 6) || (r === 6 && c === size - 7) || (r === size - 7 && c === 6)) continue;
      for (let dr = -2; dr <= 2; dr++) {
        for (let dc = -2; dc <= 2; dc++) {
          const ring = Math.max(Math.abs(dr), Math.abs(dc));
          m[r + dr][c + dc] = (ring === 1) ? 0 : 1;
        }
      }
    }
  }
}

function placeFunctionPatterns(m, version) {
  const size = m.length;
  placeFinder(m, 0, 0);
  placeFinder(m, 0, size - 7);
  placeFinder(m, size - 7, 0);
  placeAlignment(m, version);

  for (let i = 8; i < size - 8; i++) {
    const bit = i % 2 === 0 ? 1 : 0;
    m[6][i] = bit;
    m[i][6] = bit;
  }
  m[size - 8][8] = 1;   // the always-dark module

  // Reserve the format areas so data skips them.
  for (let i = 0; i < 9; i++) {
    if (m[8][i] === null) m[8][i] = 0;
    if (m[i][8] === null) m[i][8] = 0;
  }
  for (let i = 0; i < 8; i++) {
    if (m[8][size - 1 - i] === null) m[8][size - 1 - i] = 0;
    if (m[size - 1 - i][8] === null) m[size - 1 - i][8] = 0;
  }
  if (version >= 7) {
    for (let i = 0; i < 18; i++) {
      const r = Math.floor(i / 3);
      const c = i % 3;
      m[r][size - 11 + c] = 0;
      m[size - 11 + c][r] = 0;
    }
  }
}

function maskBit(mask, r, c) {
  switch (mask) {
    case 0: return (r + c) % 2 === 0;
    case 1: return r % 2 === 0;
    case 2: return c % 3 === 0;
    case 3: return (r + c) % 3 === 0;
    case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
    case 5: return ((r * c) % 2) + ((r * c) % 3) === 0;
    case 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
    default: return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0;
  }
}

function placeData(m, reserved, bytes, mask) {
  const size = m.length;
  let bitIndex = 0;
  let upward = true;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;             // the vertical timing column is skipped
    for (let step = 0; step < size; step++) {
      const row = upward ? size - 1 - step : step;
      for (const col of [right, right - 1]) {
        if (reserved[row][col]) continue;
        let bit = 0;
        if (bitIndex < bytes.length * 8) {
          bit = (bytes[bitIndex >> 3] >> (7 - (bitIndex & 7))) & 1;
        }
        bitIndex++;
        m[row][col] = maskBit(mask, row, col) ? bit ^ 1 : bit;
      }
    }
    upward = !upward;
  }
}

function placeFormat(m, mask) {
  const size = m.length;
  const bits = formatBits(mask);
  // The 15 format bits go out most-significant first: get(0) is bit 14.
  const get = (i) => (bits >> (14 - i)) & 1;

  for (let i = 0; i <= 5; i++) m[8][i] = get(i);
  m[8][7] = get(6);
  m[8][8] = get(7);
  m[7][8] = get(8);
  for (let i = 9; i <= 14; i++) m[14 - i][8] = get(i);

  for (let i = 0; i <= 7; i++) m[size - 1 - i][8] = get(i);
  for (let i = 8; i <= 14; i++) m[8][size - 15 + i] = get(i);
  m[size - 8][8] = 1;
}

function placeVersion(m, version) {
  if (version < 7) return;
  const size = m.length;
  const bits = versionBits(version);
  for (let i = 0; i < 18; i++) {
    const bit = (bits >> i) & 1;
    const r = Math.floor(i / 3);
    const c = i % 3;
    m[r][size - 11 + c] = bit;
    m[size - 11 + c][r] = bit;
  }
}

/* ---- mask scoring (spec penalty rules 1–4) ---- */

function penalty(m) {
  const size = m.length;
  let score = 0;

  // Rule 1: runs of five or more same-coloured modules in a line.
  for (let i = 0; i < size; i++) {
    for (const horizontal of [true, false]) {
      let run = 1;
      for (let j = 1; j < size; j++) {
        const cur = horizontal ? m[i][j] : m[j][i];
        const prev = horizontal ? m[i][j - 1] : m[j - 1][i];
        if (cur === prev) {
          run++;
        } else {
          if (run >= 5) score += run - 2;
          run = 1;
        }
      }
      if (run >= 5) score += run - 2;
    }
  }

  // Rule 2: 2x2 blocks of one colour.
  for (let r = 0; r < size - 1; r++) {
    for (let c = 0; c < size - 1; c++) {
      const v = m[r][c];
      if (v === m[r][c + 1] && v === m[r + 1][c] && v === m[r + 1][c + 1]) score += 3;
    }
  }

  // Rule 3: the finder-like 1:1:3:1:1 pattern with four light modules beside it.
  const a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
  const b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
  const at = (r, c) => (r < 0 || r >= size || c < 0 || c >= size ? 0 : m[r][c]);
  for (let i = 0; i < size; i++) {
    for (let j = 0; j + 11 <= size; j++) {
      let ha = true; let hb = true; let va = true; let vb = true;
      for (let k = 0; k < 11; k++) {
        if (at(i, j + k) !== a[k]) ha = false;
        if (at(i, j + k) !== b[k]) hb = false;
        if (at(j + k, i) !== a[k]) va = false;
        if (at(j + k, i) !== b[k]) vb = false;
      }
      if (ha) score += 40;
      if (hb) score += 40;
      if (va) score += 40;
      if (vb) score += 40;
    }
  }

  // Rule 4: deviation from a 50/50 dark ratio.
  let dark = 0;
  for (const row of m) for (const v of row) dark += v;
  const percent = (dark * 100) / (size * size);
  score += Math.floor(Math.abs(percent - 50) / 5) * 10;
  return score;
}

/**
 * Encode `text` as a QR matrix.
 * Returns { size, modules } where modules[row][col] is 0 or 1, or null if the
 * text does not fit in versions 1–10.
 */
export function encodeQr(text, { forceMask = null } = {}) {
  const bytes = Array.from(new TextEncoder().encode(text));
  const version = chooseVersion(bytes.length);
  if (version === null) return null;

  const finalBytes = interleave(encodeData(bytes, version), version);
  const size = version * 4 + 17;

  // A reserved map marks every function-pattern module so data skips them.
  const base = emptyMatrix(size);
  placeFunctionPatterns(base, version);
  const reserved = base.map((row) => row.map((v) => v !== null));

  let best = null;
  let bestScore = Infinity;
  for (let mask = 0; mask < 8; mask++) {
    if (forceMask !== null && mask !== forceMask) continue;
    const m = base.map((row) => row.slice());
    placeData(m, reserved, finalBytes, mask);
    placeFormat(m, mask);
    placeVersion(m, version);
    const score = penalty(m);
    if (score < bestScore) { bestScore = score; best = m; }
  }
  return { size, version, modules: best };
}

/**
 * Render `text` as an inline SVG element with a quiet zone.
 * Returns null when the text does not fit, so callers can fall back.
 */
export function renderQr(text, { quiet = 4, className = '' } = {}) {
  const qr = encodeQr(text);
  if (!qr) return null;
  const total = qr.size + quiet * 2;

  // One path of rectangles is far cheaper to lay out than N elements.
  let d = '';
  for (let r = 0; r < qr.size; r++) {
    for (let c = 0; c < qr.size; c++) {
      if (qr.modules[r][c]) d += `M${c + quiet} ${r + quiet}h1v1h-1z`;
    }
  }

  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${total} ${total}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'QR code to join this game');
  svg.setAttribute('shape-rendering', 'crispEdges');
  if (className) svg.setAttribute('class', className);

  const bg = document.createElementNS(NS, 'rect');
  bg.setAttribute('width', String(total));
  bg.setAttribute('height', String(total));
  bg.setAttribute('fill', '#FDFCF8');
  svg.appendChild(bg);

  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d);
  path.setAttribute('fill', '#15140F');
  svg.appendChild(path);

  return svg;
}
