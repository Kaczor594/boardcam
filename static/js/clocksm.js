// Pure chess clock state machine. No DOM, no network, no timers of its own —
// every method takes an explicit monotonic `now` (the caller passes
// performance.now() in the browser, or any fake number in tests).
//
// States: ready -> running (turn: 'white'|'black') <-> paused -> flagged / stopped.
//
// Chess convention: white moves first, and a player presses the clock after
// their own move is made — the press hands the clock to the opponent. So the
// very first press is white's, and it starts black's clock running.
//
// The increment is credited to the side that just pressed (the mover), on
// every press including the first.
//
// PROTOCOL.md ties `seq` to the clock's own count of presses/flag/stop events
// that trigger a camera capture (§3.1, §4): seq=1 is clock.start (white's
// first press), and it increments by one on every later capturing event. This
// class only counts *presses* (`press()` calls that succeed) in `seq`; the
// caller (clock.js) is responsible for emitting the right event type
// (clock.start vs clock.press) using the `started` flag returned by press(),
// and for driving clock.flag/clock.stop sends off this class's state changes.

const SIDES = ['white', 'black'];

function other(side) {
  return side === 'white' ? 'black' : 'white';
}

export class ClockSM {
  constructor({ initialMs, incrementMs }) {
    this.initialMs = initialMs;
    this.incrementMs = incrementMs;
    this._reset();
  }

  _reset() {
    this._state = 'ready';
    this._turn = null;
    this._ms = { white: this.initialMs, black: this.initialMs };
    this._lastTs = null;
    this._seq = 0;
    this._result = null;
    this._flaggedSide = null;
  }

  get state() { return this._state; }
  get turn() { return this._turn; }
  get seq() { return this._seq; }
  get result() { return this._result; }
  get flaggedSide() { return this._flaggedSide; }

  /** ms left for `side` at time `now`. Does not mutate state. */
  remaining(side, now) {
    if (this._state === 'running' && this._turn === side) {
      return Math.max(0, this._ms[side] - (now - this._lastTs));
    }
    return this._ms[side];
  }

  /**
   * Register a press by `side` at time `now`.
   * Returns null if the press is illegal or ignored, else
   * {seq, side, whiteMs, blackMs, started}.
   */
  press(side, now) {
    if (!SIDES.includes(side)) return null;

    if (this._state === 'ready') {
      if (side !== 'white') return null; // white presses first, always
      return this._applyPress(side, now, /* fromReady */ true);
    }

    if (this._state === 'running') {
      if (side !== this._turn) return null; // only the side to move may press
      // Defensive: if time has already run out for the running side (e.g.
      // this press and a missed tick() race), flag instead of pressing.
      if (this.remaining(this._turn, now) <= 0) {
        this._flag(this._turn, now);
        return null;
      }
      return this._applyPress(side, now, /* fromReady */ false);
    }

    // paused, flagged, stopped: every press is ignored.
    return null;
  }

  _applyPress(side, now, fromReady) {
    const elapsed = fromReady ? 0 : now - this._lastTs;
    const newRemaining = this._ms[side] - elapsed;
    if (newRemaining <= 0) {
      this._flag(side, now);
      return null;
    }
    this._ms[side] = newRemaining + this.incrementMs;
    this._state = 'running';
    this._turn = other(side);
    this._lastTs = now;
    this._seq += 1;
    return {
      seq: this._seq,
      side,
      whiteMs: this._ms.white,
      blackMs: this._ms.black,
      started: fromReady,
    };
  }

  /** Called from the rAF loop. Returns {flagged: side} once, else null. */
  tick(now) {
    if (this._state !== 'running') return null;
    if (this.remaining(this._turn, now) <= 0) {
      const side = this._turn;
      this._flag(side, now);
      return { flagged: side };
    }
    return null;
  }

  _flag(side, now) {
    this._ms[side] = 0;
    this._state = 'flagged';
    this._flaggedSide = side;
    this._turn = null;
    this._lastTs = now;
  }

  /** Freezes the running clock. No-op (returns false) unless running. */
  pause(now) {
    if (this._state !== 'running') return false;
    this._ms[this._turn] = this.remaining(this._turn, now);
    this._state = 'paused';
    this._lastTs = now;
    return true;
  }

  /** Restarts the clock for whichever side was to move. No-op unless paused. */
  resume(now) {
    if (this._state !== 'paused') return false;
    this._state = 'running';
    this._lastTs = now;
    return true;
  }

  /** Ends the game. Legal from any state except already-stopped. */
  stop(result, now) {
    if (this._state === 'stopped') return false;
    if (this._state === 'running') {
      this._ms[this._turn] = this.remaining(this._turn, now);
    }
    this._state = 'stopped';
    this._result = result;
    this._turn = null;
    this._lastTs = now;
    return true;
  }

  /** Back to `ready` with fresh times. Legal from any state. */
  reset(now) {
    this._reset();
    this._lastTs = now;
    return true;
  }

  /** Change the time control. Only legal in `ready`. */
  configure({ initialMs, incrementMs }) {
    if (this._state !== 'ready') return false;
    this.initialMs = initialMs;
    this.incrementMs = incrementMs;
    this._ms = { white: initialMs, black: initialMs };
    return true;
  }
}
