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

  /**
   * Everything needed to rebuild this clock after the page is reloaded.
   * `now` is the monotonic time (performance.now()), `wallNow` is Date.now():
   * the monotonic clock restarts with the page, wall time does not.
   */
  snapshot(now, wallNow) {
    return {
      v: 1,
      initialMs: this.initialMs,
      incrementMs: this.incrementMs,
      state: this._state,
      turn: this._turn,
      ms: { white: this.remaining('white', now), black: this.remaining('black', now) },
      wall: wallNow,
      seq: this._seq,
      result: this._result,
      flaggedSide: this._flaggedSide,
    };
  }

  /**
   * A clock rebuilt from snapshot(). A running clock keeps running: the wall
   * time that passed while the page was gone is charged to the side to move,
   * exactly as if the page had stayed open. A paused clock comes back with the
   * times it was paused at. Returns null for anything that is not a snapshot.
   */
  static restore(snap, now, wallNow) {
    if (!snap || snap.v !== 1 || !snap.ms) return null;
    const sm = new ClockSM({ initialMs: snap.initialMs, incrementMs: snap.incrementMs });
    sm._state = snap.state;
    sm._turn = snap.turn;
    sm._ms = { white: snap.ms.white, black: snap.ms.black };
    sm._seq = snap.seq;
    sm._result = snap.result;
    sm._flaggedSide = snap.flaggedSide;
    const away = Math.max(0, wallNow - snap.wall);   // a clock set backwards is not negative time
    sm._lastTs = snap.state === 'running' ? now - away : now;
    return sm;
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

/**
 * Whether the camera button should glow: the plies since the last photo have
 * reached the threshold past which the moves in between are hard to
 * reconstruct (engine/solver.json `hint_plies`, sent in `hello`).
 *
 * Off before the first photo of the game, off unless the clock is running or
 * paused, and off while either player has under one minute — a nudge must not
 * compete with a time scramble. The next photo moves `lastPhotoPly` and so
 * clears it.
 */
export const HINT_MIN_MS = 60000;

export function photoHint({ state, ply, lastPhotoPly, threshold, whiteMs, blackMs }) {
  if (lastPhotoPly == null || !threshold) return false;
  if (state !== 'running' && state !== 'paused') return false;
  if (Math.min(whiteMs, blackMs) < HINT_MIN_MS) return false;
  return ply - lastPhotoPly >= threshold;
}
