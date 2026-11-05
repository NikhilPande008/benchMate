// Session rotation and reconnect against a fake transport (node --test).

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { BACK_NOTE, Backoff, naturalPause, rotationDue, SessionManager, type LiveView, type RotationConfig, type Summary, type VoiceSession } from './rotation.ts';

const CFG: RotationConfig = { enabled: true, max_turns: 25, max_minutes: 30, max_context_tokens: 24000 };

class FakeSession implements VoiceSession {
  static n = 0;
  readonly id = `rt_${++FakeSession.n}`;
  log: string[] = [];
  view: LiveView = { audioPlaying: false, responsesInFlight: 0, toolsPending: 0, pttDown: false, lastActivity: 0 };
  tokens = 0;
  private drop: ((r: string) => void) | null = null;
  activate() {
    this.log.push('activate');
  }
  deactivate() {
    this.log.push('deactivate');
  }
  close() {
    this.log.push('close');
  }
  inject(text: string, respond: boolean) {
    this.log.push(`inject:${respond ? 'respond:' : ''}${text}`);
  }
  onDrop(cb: (r: string) => void) {
    this.drop = cb;
  }
  live() {
    return this.view;
  }
  contextTokens() {
    return this.tokens;
  }
  fail(reason = 'pc_failed') {
    this.drop?.(reason);
  }
}

function setup(opts: { failOpens?: number; pause?: boolean; cfg?: RotationConfig } = {}) {
  const clock = { ms: 1_000_000 };
  const opened: FakeSession[] = [];
  const reports: [string, Record<string, unknown>][] = [];
  const sleeps: number[] = [];
  let fails = opts.failOpens ?? 0;
  const summary: Summary = { text: 'Session summary: demo phone, dead.', pause_ok: opts.pause ?? true };
  const m = new SessionManager(opts.cfg ?? CFG, {
    open: async () => {
      if (fails > 0) {
        fails -= 1;
        throw new Error('mint failed');
      }
      const s = new FakeSession();
      opened.push(s);
      return s;
    },
    summary: async () => summary,
    report: (e, a) => reports.push([e, a]),
    now: () => clock.ms,
    sleep: async (ms) => {
      sleeps.push(ms);
      clock.ms += ms;
    },
    random: () => 0.5,
  });
  const first = new FakeSession();
  m.adopt(first);
  return { m, first, opened, reports, sleeps, clock, summary };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

test('rotation is due after the turn limit, the time limit or a large context, and never when off', () => {
  assert.equal(rotationDue(CFG, 24, 0, 0, 0), null);
  assert.equal(rotationDue(CFG, 25, 0, 0, 0), 'turns');
  assert.equal(rotationDue(CFG, 3, 0, 0, 30 * 60_000), 'time');
  assert.equal(rotationDue(CFG, 3, 0, 24_000, 0), 'tokens');
  assert.equal(rotationDue({ ...CFG, enabled: false }, 99, 0, 99_999, 1e9), null);
});

test('a natural pause: nothing playing or in flight, talk key up, quiet for a moment', () => {
  const quiet: LiveView = { audioPlaying: false, responsesInFlight: 0, toolsPending: 0, pttDown: false, lastActivity: 0 };
  assert.equal(naturalPause(quiet, 2000), true);
  assert.equal(naturalPause(quiet, 1000), false); // just spoke
  for (const busy of [{ audioPlaying: true }, { responsesInFlight: 1 }, { toolsPending: 1 }, { pttDown: true }]) {
    assert.equal(naturalPause({ ...quiet, ...busy }, 5000), false, JSON.stringify(busy));
  }
});

test('backoff doubles to a cap, with jitter', () => {
  const b = new Backoff(() => 0.5);
  assert.deepEqual([b.next(), b.next(), b.next(), b.next(), b.next(), b.next()], [500, 1000, 2000, 4000, 8000, 8000]);
  const lo = new Backoff(() => 0).next();
  const hi = new Backoff(() => 1).next();
  assert.ok(lo === 400 && hi === 600);
});

test('rotation pre-opens in the background, then swaps at a pause, seeded with the summary', async () => {
  const { m, first, opened, reports } = setup();
  for (let i = 0; i < 25; i++) m.turnDone();
  assert.equal(await m.tick(), null); // starts pre-opening; no swap yet
  await flush();
  assert.equal(opened.length, 1);
  const next = opened[0];
  assert.equal(m.standby, next);
  assert.deepEqual(next.log, []); // muted standby: nothing yet
  assert.equal(await m.tick(), 'turns');
  assert.equal(m.active, next);
  assert.deepEqual(next.log, ['inject:[engine] Session summary: demo phone, dead.', 'activate']);
  assert.deepEqual(first.log, ['deactivate', 'close']);
  assert.equal(m.turns, 0);
  assert.equal(m.rotations, 1);
  assert.equal(reports.at(-1)![0], 'rotated');
  assert.equal(reports.at(-1)![1].reason, 'turns');
});

test('no swap while the user talks or the engine says not now', async () => {
  const { m, first, opened } = setup({ pause: false });
  for (let i = 0; i < 25; i++) m.turnDone();
  await m.tick();
  await flush();
  assert.equal(await m.tick(), null); // engine: an approval is live
  assert.equal(m.active, first);
  const s2 = setup();
  for (let i = 0; i < 25; i++) s2.m.turnDone();
  await s2.m.tick();
  await flush();
  s2.first.view = { ...s2.first.view, pttDown: true };
  assert.equal(await s2.m.tick(), null);
  assert.equal(s2.m.active, s2.first);
  assert.ok(opened.length === 1 && m.standby === opened[0]); // the standby waits for the next pause
});

test('a drop hands over to the ready standby at once, and it says it is back', async () => {
  const { m, first, opened } = setup();
  await m.prepare();
  const standby = opened[0];
  first.fail('pc_failed');
  await flush();
  await flush();
  assert.equal(m.active, standby);
  assert.equal(m.reconnects, 1);
  assert.deepEqual(standby.log, ['inject:[engine] Session summary: demo phone, dead.', `inject:respond:${BACK_NOTE}`, 'activate']);
  assert.ok(first.log.includes('close'));
});

test('without a standby, a drop reconnects with backoff', async () => {
  const { m, first, opened, sleeps, reports } = setup({ failOpens: 3 });
  first.fail('pc_disconnected');
  for (let i = 0; i < 10; i++) await flush();
  assert.deepEqual(sleeps, [500, 1000, 2000]);
  assert.equal(opened.length, 1);
  assert.equal(m.active, opened[0]);
  assert.ok(opened[0].log.includes(`inject:respond:${BACK_NOTE}`));
  assert.deepEqual(reports.map((r) => r[0]), ['connection_dropped', 'reconnect_failed', 'reconnect_failed', 'reconnect_failed', 'rotated']);
});

test('reconnect gives up after the attempt limit', async () => {
  const { m, first, reports } = setup({ failOpens: 99 });
  m.maxAttempts = 3;
  first.fail('pc_failed');
  for (let i = 0; i < 12; i++) await flush();
  assert.equal(m.active, null);
  assert.equal(reports.at(-1)![0], 'reconnect_gave_up');
});

test('a dropped standby is forgotten; a closed manager stops', async () => {
  const { m, opened } = setup();
  await m.prepare();
  (opened[0] as FakeSession).fail();
  assert.equal(m.standby, null);
  m.close();
  assert.equal(await m.tick(), null);
});
