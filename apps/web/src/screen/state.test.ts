// Companion screen state reducer (node --test).

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { apply, approvalLeft, EMPTY, fmtValue, outcomeWord, type ScreenState } from './state.ts';

const base: ScreenState = { ...EMPTY, session_id: 'se', approval: { id: 'pw_1', step: 'connect_boot_cable', volts: 4, limit_a: 1, granted_ms: 1000, ttl_s: 20, speakable: true } };

test('a screen.state replaces everything', () => {
  const s = apply(base, { event: 'screen.state', state: { ...EMPTY, session_id: 'se', state: 'probing' } });
  assert.equal(s.state, 'probing');
  assert.equal(s.approval, null);
});

test('watch samples build the live boot trace; a new watch starts a new one', () => {
  let s = apply(base, { event: 'watch.sample', t_s: 0, current_a: 0.1, limit_a: 1 });
  s = apply(s, { event: 'watch.sample', t_s: 0.5, current_a: 0.2, limit_a: 1 });
  assert.deepEqual(s.boot?.samples, [[0, 0.1], [0.5, 0.2]]);
  assert.equal(s.boot?.live, true);
  const done = { ...s, boot: { ...s.boot!, live: false, pattern: 'NORMAL_BOOT' } };
  assert.deepEqual(apply(done, { event: 'watch.sample', t_s: 0, current_a: 0.8 }).boot?.samples, [[0, 0.8]]);
});

test('transcript lines and interruptions are kept, newest last', () => {
  let s = apply(base, { event: 'transcript', role: 'user', text: 'meter padho', lang: 'hinglish' });
  s = apply(s, { event: 'interruption', what: 'barge_in' });
  assert.deepEqual(s.transcript.map((l) => [l.role, l.text]), [['user', 'meter padho'], ['event', 'interrupted']]);
});

test('power.ended clears only its own approval', () => {
  assert.notEqual(apply(base, { event: 'power.ended', id: 'pw_other', reason: 'renewed' }).approval, null);
  assert.equal(apply(base, { event: 'power.ended', id: 'pw_1', reason: 'barge_in' }).approval, null);
});

test('language, vision level and safety stop', () => {
  assert.equal(apply(base, { event: 'session.lang', lang: 'hi', decided: true }).lang, 'hi');
  assert.equal(apply(base, { event: 'vision.level', level: 'reader_b_only', reason: 'vision_api' }).vision.level, 'reader_b_only');
  const stopped = apply(base, { event: 'safety.stop', reason: 'battery or smoke reported' });
  assert.equal(stopped.stopped, 'battery or smoke reported');
  assert.equal(stopped.approval, null);
});

test('approval countdown', () => {
  assert.equal(approvalLeft(base.approval!, 1000), 20);
  assert.equal(approvalLeft(base.approval!, 15_500), 6);
  assert.equal(approvalLeft(base.approval!, 30_000), 0);
});

test('every status colour comes with a word', () => {
  for (const o of ['normal', 'short', 'open', 'low', 'AT_LIMIT', 'cap_hot', 'something_new']) {
    const [word, tone] = outcomeWord(o);
    assert.ok(word.length > 1 && ['ok', 'warn', 'bad', 'info'].includes(tone), o);
  }
  assert.deepEqual(outcomeWord('short'), ['SHORT', 'bad']);
});

test('values', () => {
  assert.equal(fmtValue(0.352, 'diode_V'), '0.352 V');
  assert.equal(fmtValue(null, 'V', true), 'OL');
  assert.equal(fmtValue(4700, 'ohm'), '4.70 k Ω');
  assert.equal(fmtValue(null, 'A'), '—');
});
