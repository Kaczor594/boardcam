import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { ClockSM } from './clocksm.js';

const INITIAL = 60000; // 60 s
const INCREMENT = 5000; // 5 s

function freshSM() {
  return new ClockSM({ initialMs: INITIAL, incrementMs: INCREMENT });
}

describe('ClockSM basic shape', () => {
  test('starts in ready with no turn and seq 0', () => {
    const sm = freshSM();
    assert.equal(sm.state, 'ready');
    assert.equal(sm.turn, null);
    assert.equal(sm.seq, 0);
    assert.equal(sm.remaining('white', 0), INITIAL);
    assert.equal(sm.remaining('black', 0), INITIAL);
  });

  test('first press must be white, and starts the game', () => {
    const sm = freshSM();
    const bad = sm.press('black', 1000);
    assert.equal(bad, null);
    assert.equal(sm.state, 'ready');

    const res = sm.press('white', 1000);
    assert.ok(res);
    assert.equal(res.started, true);
    assert.equal(res.seq, 1);
    assert.equal(res.side, 'white');
    assert.equal(sm.state, 'running');
    assert.equal(sm.turn, 'black'); // black's clock now runs
  });
});

describe('increment', () => {
  test('increment is credited to the side that just pressed', () => {
    const sm = freshSM();
    // white presses at t=1000 having used no time yet (fromReady => no elapsed deduction)
    const res1 = sm.press('white', 1000);
    assert.equal(res1.whiteMs, INITIAL + INCREMENT);
    assert.equal(res1.blackMs, INITIAL);

    // black thinks for 3000ms then presses at t=4000
    const res2 = sm.press('black', 4000);
    assert.ok(res2);
    assert.equal(res2.side, 'black');
    // black's remaining before increment: INITIAL - 3000ms elapsed, then + increment
    assert.equal(res2.blackMs, INITIAL - 3000 + INCREMENT);
    // white's time is unaffected by black's press
    assert.equal(res2.whiteMs, INITIAL + INCREMENT);
  });

  test('remaining() reflects (time at press) + increment right after pressing', () => {
    const sm = freshSM();
    sm.press('white', 0);
    // black presses after thinking 10000ms
    const now = 10000;
    const res = sm.press('black', now);
    assert.equal(sm.remaining('black', now), INITIAL - 10000 + INCREMENT);
  });
});

describe('flagging', () => {
  test('running the clock past the initial time flags the correct side', () => {
    const sm = freshSM();
    sm.press('white', 0); // black's clock now runs from t=0, has INITIAL ms
    const tickResult = sm.tick(INITIAL + 1);
    assert.deepEqual(tickResult, { flagged: 'black' });
    assert.equal(sm.state, 'flagged');
    assert.equal(sm.flaggedSide, 'black');
  });

  test('no further presses are accepted after a flag', () => {
    const sm = freshSM();
    sm.press('white', 0);
    sm.tick(INITIAL + 1);
    assert.equal(sm.state, 'flagged');
    const seqBefore = sm.seq;
    assert.equal(sm.press('black', INITIAL + 100), null);
    assert.equal(sm.press('white', INITIAL + 100), null);
    assert.equal(sm.seq, seqBefore);
    assert.equal(sm.state, 'flagged');
  });

  test('a press that arrives after time has already expired flags instead of scoring', () => {
    const sm = freshSM();
    sm.press('white', 0); // black to move, black has INITIAL ms from t=0
    // black "presses" at a time past their flag point without tick() having run first
    const res = sm.press('black', INITIAL + 500);
    assert.equal(res, null);
    assert.equal(sm.state, 'flagged');
    assert.equal(sm.flaggedSide, 'black');
  });
});

describe('pause excludes elapsed time', () => {
  test('pausing for 5000ms then resuming leaves remaining time unchanged across the pause', () => {
    const sm = freshSM();
    sm.press('white', 0); // black running from t=0
    const beforePause = sm.remaining('black', 2000); // black thought for 2000ms
    assert.ok(sm.pause(2000));
    assert.equal(sm.state, 'paused');
    // remaining() should be stable while paused regardless of `now`
    assert.equal(sm.remaining('black', 7000), beforePause);

    assert.ok(sm.resume(7000)); // resumed after a 5000ms real-world pause
    assert.equal(sm.state, 'running');
    // immediately after resume, remaining is unchanged from the pause point
    assert.equal(sm.remaining('black', 7000), beforePause);
    // and it continues ticking down normally afterward
    assert.equal(sm.remaining('black', 7100), beforePause - 100);
  });

  test('pause/resume are no-ops in the wrong state', () => {
    const sm = freshSM();
    assert.equal(sm.pause(0), false); // not running yet
    assert.equal(sm.resume(0), false); // not paused
    sm.press('white', 0);
    assert.equal(sm.resume(100), false); // running, not paused
  });
});

describe('press count / seq', () => {
  test('N legal presses produce seq 1..N with no gaps, first press is white', () => {
    const sm = freshSM();
    const sides = ['white', 'black', 'white', 'black', 'white'];
    let t = 0;
    const seqs = [];
    for (const side of sides) {
      t += 1000;
      const res = sm.press(side, t);
      assert.ok(res, `press by ${side} at ${t} should be legal`);
      seqs.push(res.seq);
    }
    assert.deepEqual(seqs, [1, 2, 3, 4, 5]);
    assert.equal(sm.seq, 5);
  });
});

describe('illegal presses are ignored', () => {
  test('wrong side to move is ignored and does not advance seq', () => {
    const sm = freshSM();
    sm.press('white', 0); // black to move now
    const before = sm.seq;
    const res = sm.press('white', 100); // white pressing again out of turn
    assert.equal(res, null);
    assert.equal(sm.seq, before);
    assert.equal(sm.turn, 'black');
  });

  test('press while paused is ignored and does not advance seq', () => {
    const sm = freshSM();
    sm.press('white', 0);
    sm.pause(500);
    const before = sm.seq;
    assert.equal(sm.press('black', 600), null);
    assert.equal(sm.seq, before);
    assert.equal(sm.state, 'paused');
  });

  test('press after stop is ignored and does not advance seq', () => {
    const sm = freshSM();
    sm.press('white', 0);
    sm.stop('1-0', 1000);
    const before = sm.seq;
    assert.equal(sm.press('black', 1100), null);
    assert.equal(sm.press('white', 1100), null);
    assert.equal(sm.seq, before);
    assert.equal(sm.state, 'stopped');
  });

  test('press before the game starts from the wrong side is ignored', () => {
    const sm = freshSM();
    assert.equal(sm.press('black', 0), null);
    assert.equal(sm.state, 'ready');
    assert.equal(sm.seq, 0);
  });
});

describe('stop and reset', () => {
  test('stop freezes state and records the result', () => {
    const sm = freshSM();
    sm.press('white', 0);
    assert.ok(sm.stop('1/2-1/2', 5000));
    assert.equal(sm.state, 'stopped');
    assert.equal(sm.result, '1/2-1/2');
  });

  test('stop is a no-op if already stopped', () => {
    const sm = freshSM();
    sm.press('white', 0);
    sm.stop('1-0', 1000);
    assert.equal(sm.stop('0-1', 2000), false);
    assert.equal(sm.result, '1-0');
  });

  test('reset returns to ready with fresh times from any state', () => {
    const sm = freshSM();
    sm.press('white', 0);
    sm.press('black', 1000);
    sm.stop('1-0', 2000);
    assert.ok(sm.reset(3000));
    assert.equal(sm.state, 'ready');
    assert.equal(sm.turn, null);
    assert.equal(sm.seq, 0);
    assert.equal(sm.result, null);
    assert.equal(sm.remaining('white', 3000), INITIAL);
    assert.equal(sm.remaining('black', 3000), INITIAL);
  });
});

describe('configure', () => {
  test('configure only applies in ready state', () => {
    const sm = freshSM();
    assert.ok(sm.configure({ initialMs: 300000, incrementMs: 0 }));
    assert.equal(sm.remaining('white', 0), 300000);
    assert.equal(sm.remaining('black', 0), 300000);

    sm.press('white', 0);
    assert.equal(sm.configure({ initialMs: 900000, incrementMs: 10000 }), false);
    // unchanged
    assert.equal(sm.remaining('black', 0), 300000);
  });
});
