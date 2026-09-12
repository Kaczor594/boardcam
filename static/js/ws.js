/* Shared client helpers: reconnecting WebSocket, fetch wrapper, wake lock,
   modals, toasts. Both phone pages import from here.
   Protocol: docs/PROTOCOL.md */

export const NGROK_HEADER = { 'ngrok-skip-browser-warning': '1' };

/** fetch() with the ngrok header and JSON handling. Throws on non-2xx. */
export async function api(path, { method = 'GET', body, headers = {} } = {}) {
  const opts = { method, headers: { ...NGROK_HEADER, ...headers } };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail ?? detail; } catch { /* not JSON */ }
    const err = new Error(`${resp.status} ${detail}`);
    err.status = resp.status;
    throw err;
  }
  const type = resp.headers.get('content-type') || '';
  return type.includes('application/json') ? resp.json() : resp.text();
}

/**
 * WebSocket that reconnects on its own and heartbeats every 10 s.
 *
 * Events (addEventListener / on*):
 *   onopen()            socket live (fires again after every reconnect)
 *   onclose(code)       socket gone; a reconnect is already scheduled
 *   onmessage(msg)      a decoded JSON object from the server
 *   onstate(state)      'connecting' | 'open' | 'closed'
 *
 * A close with code 4000 means another device took this role: we do NOT
 * reconnect, because fighting over the slot would flap forever.
 */
export class GameSocket {
  constructor(room, role) {
    this.room = room;
    this.role = role;
    this.ws = null;
    this.state = 'closed';
    this.backoff = 300;
    this.pongMisses = 0;
    this.replaced = false;
    this.stopped = false;
    this.onopen = () => {};
    this.onclose = () => {};
    this.onmessage = () => {};
    this.onstate = () => {};
    this._timers = [];
  }

  url() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${location.host}/ws/${encodeURIComponent(this.room)}?role=${this.role}`;
  }

  _setState(s) {
    if (this.state !== s) { this.state = s; this.onstate(s); }
  }

  connect() {
    if (this.stopped || this.replaced) return;
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;

    this._setState('connecting');
    let ws;
    try {
      ws = new WebSocket(this.url());
    } catch {
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.backoff = 300;
      this.pongMisses = 0;
      this._setState('open');
      this._startHeartbeat();
      this.onopen();
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg && msg.type === 'pong') { this.pongMisses = 0; return; }
      if (msg) this.onmessage(msg);
    };

    ws.onclose = (ev) => {
      this._stopHeartbeat();
      this._setState('closed');
      if (ev.code === 4000) {
        // Replaced by another socket in this role: stay down.
        this.replaced = true;
        this.onclose(ev.code);
        return;
      }
      this.onclose(ev.code);
      this._scheduleReconnect();
    };

    ws.onerror = () => { /* onclose always follows; nothing useful to do here */ };
  }

  _scheduleReconnect() {
    if (this.stopped || this.replaced) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 1.8, 5000);
    this._timers.push(setTimeout(() => this.connect(), delay));
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    this._hb = setInterval(() => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      // Two silent pongs in a row means the tunnel is gone but the socket
      // has not noticed. Force it.
      if (this.pongMisses >= 2) { try { this.ws.close(); } catch { /* already gone */ } return; }
      this.pongMisses += 1;
      this.send({ type: 'ping' });
    }, 10000);
  }

  _stopHeartbeat() {
    if (this._hb) { clearInterval(this._hb); this._hb = null; }
  }

  /** Returns true if the message went out; false if the socket was down. */
  send(msg) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try { this.ws.send(JSON.stringify(msg)); return true; } catch { return false; }
  }

  close() {
    this.stopped = true;
    this._stopHeartbeat();
    this._timers.forEach(clearTimeout);
    if (this.ws) { try { this.ws.close(); } catch { /* already gone */ } }
  }
}

/* ---------------- wake lock ---------------- */

/**
 * Holds a screen wake lock, re-acquiring it whenever the page returns to the
 * foreground (iOS drops it on every visibility change). Safe to call where
 * the API is missing.
 */
export function keepAwake() {
  let lock = null;
  let released = false;

  async function acquire() {
    if (released || !('wakeLock' in navigator)) return;
    try {
      lock = await navigator.wakeLock.request('screen');
      lock.addEventListener('release', () => { lock = null; });
    } catch { lock = null; }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !lock) acquire();
  });
  acquire();

  return { release() { released = true; if (lock) { lock.release().catch(() => {}); lock = null; } } };
}

/* ---------------- UI helpers ---------------- */

export function setDot(el, cls) {
  if (!el) return;
  el.classList.remove('ok', 'warn', 'error');
  if (cls) el.classList.add(cls);
}

export function pulseDot(el) {
  if (!el) return;
  el.classList.remove('pulse');
  void el.offsetWidth; // restart the animation
  el.classList.add('pulse');
}

let toastTimer = null;
export function toast(text, ms = 2600) {
  let el = document.querySelector('.toast');
  if (!el) { el = document.createElement('div'); el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

/**
 * Custom modal. Never window.confirm() — on iOS a native dialog blocks the
 * event loop and can kill the camera stream.
 *
 * choices: [{label, value, cls}]. Resolves with the chosen value, or null if
 * dismissed by tapping the backdrop.
 */
export function modal({ title, body = '', choices, row = false, dismissible = true }) {
  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    const box = document.createElement('div');
    box.className = 'modal';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    const h = document.createElement('h2');
    h.textContent = title;
    box.appendChild(h);

    if (body) {
      const p = document.createElement('div');
      p.className = 'modal-body';
      p.textContent = body;
      box.appendChild(p);
    }

    const actions = document.createElement('div');
    actions.className = 'modal-actions' + (row ? ' row' : '');
    for (const c of choices) {
      const b = document.createElement('button');
      b.className = 'btn ' + (c.cls || '');
      b.textContent = c.label;
      b.addEventListener('click', () => { done(c.value); });
      actions.appendChild(b);
    }
    box.appendChild(actions);
    backdrop.appendChild(box);

    if (dismissible) {
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) done(null); });
    }

    function done(value) {
      backdrop.remove();
      document.removeEventListener('keydown', onKey);
      resolve(value);
    }
    function onKey(e) { if (dismissible && e.key === 'Escape') done(null); }
    document.addEventListener('keydown', onKey);

    document.body.appendChild(backdrop);
  });
}

/* ---------------- formatting ---------------- */

/** 600000 -> "10:00"; under a minute -> "9.4" (one decimal, chess convention). */
export function formatClock(ms) {
  if (ms <= 0) return '0:00';
  const total = ms / 1000;
  if (total < 20) return total.toFixed(1);
  const s = Math.ceil(total);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
  return (h > 0 ? `${h}:` : '') + `${mm}:${String(sec).padStart(2, '0')}`;
}

/** "0:09:57.3" for PGN %clk tags. */
export function formatClkTag(ms) {
  const total = Math.max(0, ms) / 1000;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = (total % 60).toFixed(1).padStart(4, '0');
  return `${h}:${String(m).padStart(2, '0')}:${s}`;
}

/** Room code from ?room=, uppercased, or ''. */
export function roomFromUrl() {
  return (new URLSearchParams(location.search).get('room') || '').toUpperCase().trim();
}
