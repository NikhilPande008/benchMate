// Power-instruction guardrail (node --test; also run by engine/tests/test_web.py).

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { APPROVAL_TTL_MS, findViolation, PowerApprovals } from './powerGuard.ts';

const UNAPPROVED: Array<[string, string]> = [
  // English
  ['Connect the boot cable now.', 'connect_boot_cable'],
  ['Zero point zero two volts, that is a short. Connect the boot cable.', 'connect_boot_cable'],
  ['Please plug in the supply to the board.', 'connect_boot_cable'],
  ['Turn on the supply.', 'connect_boot_cable'],
  ['Apply power to the board now.', 'connect_boot_cable'],
  ['Press the power key now.', 'press_power_key'],
  ['Hold the power button for three seconds.', 'press_power_key'],
  ['Connect the supply to the charging port.', 'connect_port_supply'],
  ['Connect the battery and try again.', 'connect_battery'],
  ['Can you connect the boot cable?', 'connect_boot_cable'],
  // Hinglish (Latin script)
  ['Zero point zero two volt, yeh short hai; boot cable connect karo.', 'connect_boot_cable'],
  ['Ab boot cable lagao.', 'connect_boot_cable'],
  ['Supply chalu karo aur current dekho.', 'connect_boot_cable'],
  ['Power button dabao.', 'press_power_key'],
  ['Charger lagao aur current batao.', 'connect_port_supply'],
  ['Battery laga do.', 'connect_battery'],
  ['Board ko supply do.', 'connect_boot_cable'],
  // Devanagari
  ['अब बूट केबल लगाओ।', 'connect_boot_cable'],
  ['सप्लाई चालू करो।', 'connect_boot_cable'],
  ['पावर बटन दबाओ।', 'press_power_key'],
  ['चार्जर लगाइए।', 'connect_port_supply'],
  ['बैटरी लगा दो।', 'connect_battery'],
];

for (const [text, kind] of UNAPPROVED) {
  test(`caught without approval: ${text}`, () => {
    assert.equal(findViolation(text)?.kind, kind);
  });
}

const HARMLESS = [
  'Power chip check karo.',
  'The power rail reads 0.4 volts.',
  'Measure the power key line in diode mode.',
  'Is the boot cable connected?',
  'Kya boot cable laga diya?',
  'Do not connect the boot cable yet.',
  "Don't connect the supply until I say so.",
  'Boot cable abhi mat lagao.',
  'बूट केबल अभी मत लगाओ।',
  'Disconnect everything now.',
  'Sab disconnect karo aur phone door rakho.',
  'Set the supply to 4.0 volts. It reads 5.0 volts.',
  'Supply 4.0 volt pe set karo.',
  'Battery nikaalo, phir batao.',
  'VBAT matlab battery ki main supply line.',
  'Boot current woh current hai jo board power aur switch on hone pe leta hai.',
  'The charging port VBUS pin reads normal.',
  'Do the heat check on the battery line.',
];

for (const text of HARMLESS) {
  test(`not an instruction: ${text}`, () => {
    assert.equal(findViolation(text), null);
  });
}

test("only the gate's exact sentence passes, not a paraphrase of the approved step", () => {
  const gate = ['Connect the boot cable now.', 'Ab boot cable lagao.'];
  assert.equal(findViolation('Ab boot cable lagao.', gate), null);
  assert.equal(findViolation('Theek hai. Ab boot cable lagao, phir batao.', gate), null);
  assert.equal(findViolation('Boot cable connect karo.', gate)?.kind, 'connect_boot_cable'); // paraphrase
  // the 4 Nov case: 14 s after an approval
  assert.equal(findViolation('Short component fault is present. Next, connect the boot cable to proceed with the next instruction.', gate)?.kind, 'connect_boot_cable');
  assert.equal(findViolation('Press the power key now.', gate)?.kind, 'press_power_key');
  assert.equal(findViolation('Connect the battery.', gate)?.kind, 'connect_battery');
});

test("the gate's own sentences pass, approved or blocked", () => {
  const blocked = ['Pehle battery nikaalo, phir batao.', 'Remove the battery first and tell me when it is out.'];
  assert.equal(findViolation('Pehle battery nikaalo, phir batao.', blocked), null);
  assert.equal(findViolation('Connect the boot cable now.', ['Connect the boot cable now.']), null);
  // ...but not an instruction added next to them
  assert.equal(findViolation('Connect the boot cable now. Then press the power key.', ['Connect the boot cable now.'])?.kind, 'press_power_key');
});

// --- approval lifetime (CLAUDE.md rule 2) -----------------------------------------------------

const APPROVED_BOOT = JSON.stringify({
  approved: true, step: 'connect_boot_cable', approval: 'pw_aaa111',
  say: { en: 'Connect the boot cable now.', hi: 'Ab boot cable lagao.' },
});
const APPROVED_PRESS = JSON.stringify({
  approved: true, step: 'press_power_key', approval: 'pw_bbb222',
  say: { en: 'Press the power key now.', hi: 'Ab power button dabao.' },
});

/** A tracker on a fake clock. */
function tracker(): { t: PowerApprovals; clock: { ms: number } } {
  const clock = { ms: 1_000_000 };
  return { t: new PowerApprovals(() => clock.ms), clock };
}

test('approvals come from gate results only', () => {
  const { t } = tracker();
  t.noteToolOutput('next_step', JSON.stringify({
    step: 'power', measurement: 'heat_check', power_step: 'connect_boot_cable', approved: true, approval: 'pw_1',
    say: { en: 'Connect the boot cable now.', hi: 'Ab boot cable lagao.' },
  }));
  assert.equal(t.check('Ab boot cable lagao. Phir batao kya garam hai.'), null);
  t.noteToolOutput('get_reading', JSON.stringify({ approved: true, step: 'press_power_key' })); // not a gate tool
  assert.equal(t.check('Press the power key.')?.kind, 'press_power_key');
});

test('the gate sentence may be repeated in later turns while the approval lasts', () => {
  // the 4 Nov replay case: "Ab boot cable lagao." again one turn after the approval
  const { t, clock } = tracker();
  t.noteToolOutput('request_power_step', APPROVED_BOOT);
  assert.equal(t.check('Ab boot cable lagao.'), null);
  clock.ms += 6_000;
  t.userTurn(false); // a new turn after the agent finished: not an ending
  assert.equal(t.check('Haan, ab boot cable lagao.'), null);
  clock.ms += 6_000;
  t.userTurn(false);
  assert.equal(t.check('Connect the boot cable now.'), null);
});

test('ends after 20 s', () => {
  const { t, clock } = tracker();
  t.noteToolOutput('request_power_step', APPROVED_BOOT);
  clock.ms += APPROVAL_TTL_MS - 1;
  assert.equal(t.check('Ab boot cable lagao.'), null);
  clock.ms += 1;
  assert.equal(t.check('Ab boot cable lagao.')?.kind, 'connect_boot_cable');
  assert.equal(t.ended.at(-1)?.reason, 'expired');
});

test('ends on barge-in', () => {
  const { t } = tracker();
  t.noteToolOutput('request_power_step', APPROVED_BOOT);
  t.userTurn(true);
  assert.equal(t.check('Ab boot cable lagao.')?.kind, 'connect_boot_cable');
  assert.equal(t.ended.at(-1)?.reason, 'barge_in');
});

test('ends when a different power action is approved', () => {
  const { t } = tracker();
  t.noteToolOutput('request_power_step', APPROVED_BOOT);
  t.noteToolOutput('request_power_step', APPROVED_PRESS);
  assert.equal(t.check('Ab power button dabao.'), null);
  assert.equal(t.check('Ab boot cable lagao.')?.kind, 'connect_boot_cable');
  assert.equal(t.ended.at(-1)?.reason, 'superseded');
});

for (const reason of ['confirmed', 'used', 'supply_changed', 'violation', 'stopped']) {
  test(`ends on the engine's power.ended (${reason})`, () => {
    const { t } = tracker();
    t.noteToolOutput('request_power_step', APPROVED_BOOT);
    t.onEngineEvent({ event: 'power.ended', id: 'pw_aaa111', reason });
    assert.equal(t.check('Ab boot cable lagao.')?.kind, 'connect_boot_cable');
    assert.equal(t.ended.at(-1)?.reason, reason);
  });
}

test('a late power.ended for an older approval does not end the newer one', () => {
  const { t } = tracker();
  t.noteToolOutput('request_power_step', JSON.stringify({ ...JSON.parse(APPROVED_BOOT), approval: 'pw_new' }));
  t.onEngineEvent({ event: 'power.ended', id: 'pw_old', reason: 'renewed' });
  assert.equal(t.check('Ab boot cable lagao.'), null);
  t.onEngineEvent({ event: 'power.approved', id: 'pw_new' }); // other events are ignored
  assert.equal(t.check('Ab boot cable lagao.'), null);
});

test('a blocked gate answer grants nothing; its sentences are allowed for that turn', () => {
  const { t } = tracker();
  t.noteToolOutput('request_power_step', JSON.stringify({
    approved: false, step: 'connect_boot_cable', reason: 'battery_not_removed',
    say: { en: 'Remove the battery first and tell me when it is out.', hi: 'Pehle battery nikaalo, phir batao.' },
  }));
  assert.equal(t.check('Remove the battery first and tell me when it is out.'), null);
  assert.equal(t.check('Connect the boot cable now.')?.kind, 'connect_boot_cable');
  t.userTurn(false);
  assert.deepEqual(t.blockSentences, []);
});

test('a blocked answer after an approval leaves the approval in force', () => {
  const { t } = tracker();
  t.noteToolOutput('request_power_step', APPROVED_BOOT);
  t.noteToolOutput('request_power_step', JSON.stringify({ approved: false, step: 'press_power_key', reason: 'stale_reading', say: { en: 'x', hi: 'y' } }));
  assert.equal(t.check('Ab boot cable lagao.'), null);
});
