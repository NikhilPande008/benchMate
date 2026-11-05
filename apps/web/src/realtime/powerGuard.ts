// Output guardrail for power instructions (CLAUDE.md rule 2 backstop).
//
// The model's own transcript is checked as it streams. A power instruction (connect the boot cable
// or the supply, press the power key, apply voltage, connect the battery) in English, Hinglish or
// Devanagari, with no live gate approval for that step, is a violation: the session interrupts
// the response, the model is told to correct itself, and the engine logs it.
//
// Matching: whole words (Devanagari: whole word, or a stem of 3+ letters at the start of a word,
// as in engine/safety/scope.py). An instruction is an object ("boot cable", "सप्लाई") and a command
// verb ("connect", "lagao", "दबाओ") within 3 words of each other in one sentence, not negated
// ("don't", "mat", "मत") and not a yes/no question ("Is the boot cable connected?").
//
// Only the gate's exact sentence may instruct power (rule 2). While an approval is live its
// sentences are allowed and are taken out of the text before matching; anything else that
// instructs power is a violation, a paraphrase of the approved step included (seen 4 Nov: "Next,
// connect the boot cable to proceed" 14 s after an approval).

export type PowerKind = 'connect_boot_cable' | 'press_power_key' | 'connect_port_supply' | 'connect_battery';

export type Violation = { kind: PowerKind; sentence: string };

const WORD = /[a-z0-9]+|[ऀ-ॣ०-ॿ]+/g; // Latin or Devanagari words; no danda
const tokens = (text: string): string[] => text.toLowerCase().match(WORD) ?? [];
const phrases = (list: string[]): string[][] => list.map(tokens);

const OBJECTS: Record<PowerKind, string[][]> = {
  press_power_key: phrases(['power key', 'power button', 'power switch', 'side button', 'on off button', 'पावर बटन', 'पावर की', 'पावर कुंजी']),
  connect_battery: phrases(['battery', 'batt', 'बैटरी', 'बैट्री']),
  connect_port_supply: phrases(['charging port', 'port', 'charger', 'usb', 'पोर्ट', 'चार्जर', 'चार्जिंग पोर्ट']),
  connect_boot_cable: phrases([
    'boot cable', 'supply', 'power supply', 'power', 'voltage', 'current', 'dc power', 'cable',
    'बूट केबल', 'केबल', 'सप्लाई', 'पावर', 'वोल्टेज', 'करंट',
  ]),
};
const CONNECT_VERBS = phrases([
  'connect', 'plug', 'plug in', 'attach', 'apply', 'turn on', 'switch on', 'power on', 'power up', 'hook up',
  'lagao', 'lagaiye', 'lagayein', 'lagaye', 'laga do', 'laga dijiye', 'jodo', 'jodiye', 'connect karo', 'connect kijiye',
  'connect karein', 'connect karo', 'on karo', 'on kijiye', 'chalu karo', 'chalu kijiye', 'apply karo', 'de do', 'dijiye',
  'लगाओ', 'लगाइए', 'लगाइये', 'लगाएं', 'लगा दो', 'लगा दीजिए', 'जोड़ो', 'जोड़िए', 'चालू करो', 'चालू कीजिए', 'ऑन करो', 'दे दो', 'दीजिए',
]);
const PRESS_VERBS = phrases([
  'press', 'push', 'hold', 'long press', 'tap', 'dabao', 'dabaiye', 'dabayein', 'daba do', 'press karo', 'press kijiye',
  'दबाओ', 'दबाइए', 'दबाएं', 'दबा दो', 'प्रेस करो',
]);
// "do" (give) is a Hindi verb only right after its object ("supply do"); in English it is "do not"
const GIVE_RIGHT_AFTER_OBJECT = phrases(['do', 'दो']);
const NEGATIONS = new Set(['not', 'dont', 'don', 'never', 'no', 'without', 'mat', 'nahi', 'nahin', 'na', 'मत', 'नहीं', 'ना', 'बिना']);
const QUESTION_STARTS = new Set(['did', 'have', 'has', 'is', 'are', 'was', 'were', 'kya', 'क्या']);

function same(word: string, term: string): boolean {
  if (/^[a-z0-9]+$/.test(term)) return word === term;
  return word === term || (term.length >= 3 && word.startsWith(term));
}

/** Start indices where the phrase occurs in the words. */
function find(words: string[], phrase: string[]): number[] {
  const out: number[] = [];
  for (let i = 0; i + phrase.length <= words.length; i++) {
    if (phrase.every((t, k) => same(words[i + k], t))) out.push(i);
  }
  return out;
}

type Hit = { start: number; end: number };
const hits = (words: string[], list: string[][]): Hit[] =>
  list.flatMap((p) => find(words, p).map((start) => ({ start, end: start + p.length })));

const near = (a: Hit, b: Hit, gap = 3) => (a.start >= b.end ? a.start - b.end : b.start - a.end) <= gap;

function negated(words: string[], verb: Hit, object: Hit): boolean {
  const from = Math.max(0, Math.min(verb.start, object.start) - 2);
  const to = Math.max(verb.end, object.end);
  return words.slice(from, to).some((w) => NEGATIONS.has(w));
}

// "switch on hone pe" (on being switched on) describes; it doesn't instruct
const DESCRIPTIVE_AFTER = new Set(['hone', 'hota', 'hoti', 'hote', 'hua', 'hue', 'hui', 'hoga', 'होने', 'होता', 'होती', 'हुआ', 'हुई']);

function kindOf(words: string[]): PowerKind | null {
  const objectHits = (kind: PowerKind) => hits(words, OBJECTS[kind]);
  const command = (v: Hit) => !DESCRIPTIVE_AFTER.has(words[v.end] ?? '');
  // "do" counts as a verb only directly after an object word ("supply do", "पावर दो")
  const gives = hits(words, GIVE_RIGHT_AFTER_OBJECT).filter((g) =>
    (Object.keys(OBJECTS) as PowerKind[]).some((k) => objectHits(k).some((o) => o.end === g.start)),
  );
  const connect = [...hits(words, CONNECT_VERBS), ...gives].filter(command);
  const press = hits(words, PRESS_VERBS).filter(command);
  const instructs = (verbs: Hit[], kind: PowerKind) =>
    objectHits(kind).some((o) => verbs.some((v) => near(v, o) && !negated(words, v, o)));

  if (instructs(press, 'press_power_key')) return 'press_power_key';
  if (instructs(connect, 'connect_battery')) return 'connect_battery';
  if (instructs(connect, 'connect_port_supply') || instructs(connect, 'connect_boot_cable')) {
    // "connect the supply to the charging port": where it goes decides the step
    return objectHits('connect_port_supply').length ? 'connect_port_supply' : 'connect_boot_cable';
  }
  return null;
}

function sentences(text: string): string[] {
  return text.split(/(?<=[.!?।\n])\s*/).map((s) => s.trim()).filter(Boolean);
}

/** The first power instruction in the text that is not one of the allowed (gate) sentences, or null. */
export function findViolation(text: string, allowedSentences: readonly string[] = []): Violation | null {
  const allowed = allowedSentences.map(tokens).filter((t) => t.length);
  for (const sentence of sentences(text)) {
    let words = tokens(sentence);
    for (const gate of allowed) {
      const at = find(words, gate)[0];
      if (at !== undefined) words = [...words.slice(0, at), ...words.slice(at + gate.length)];
    }
    if (!words.length || QUESTION_STARTS.has(words[0])) continue;
    const kind = kindOf(words);
    if (kind) return { kind, sentence };
  }
  return null;
}

export const APPROVAL_TTL_MS = 20_000;

export type EndReason =
  | 'confirmed'
  | 'used'
  | 'expired'
  | 'supply_changed'
  | 'barge_in'
  | 'superseded'
  | 'renewed'
  | 'violation'
  | 'stopped';

export type LiveApproval = { step: string; id: string | null; sentences: string[]; at: number };

/**
 * The gate's live approval, with the lifetime of CLAUDE.md rule 2 (engine/safety/approval.py is
 * the authority). An approval covers one power action; while it lasts, its exact sentence may be
 * spoken again, in later turns too. It ends:
 * - after 20 s, or on barge-in, or when a different action is approved: handled here at once;
 * - on user confirmation, the measurement starting, a supply settings change, a violation or a
 *   safety stop: the engine's `power.ended` event, matched by approval id so a late event never
 *   ends a newer approval.
 * Block sentences ("Pehle battery nikaalo") are the gate's words too; they are allowed for the turn
 * they came in (they never instruct power anyway).
 */
export class PowerApprovals {
  current: LiveApproval | null = null;
  blockSentences: string[] = [];
  ended: { step: string; id: string | null; reason: EndReason; at: number }[] = [];

  private now: () => number;
  readonly ttlMs: number;

  constructor(now: () => number = () => Date.now(), ttlMs = APPROVAL_TTL_MS) {
    this.now = now;
    this.ttlMs = ttlMs;
  }

  noteToolOutput(tool: string, output: string): void {
    if (tool !== 'request_power_step' && tool !== 'next_step') return;
    let o: { approved?: boolean; step?: string; power_step?: string; say?: Record<string, string>; approval?: string };
    try {
      o = JSON.parse(output);
    } catch {
      return;
    }
    const step = o.power_step ?? o.step;
    const said = o.say && typeof o.say === 'object' ? Object.values(o.say).filter((x) => typeof x === 'string') : [];
    if (o.approved === true && step) {
      if (this.live()) this.end(this.current!.step === step ? 'renewed' : 'superseded');
      this.current = { step, id: o.approval ?? null, sentences: said, at: this.now() };
    } else {
      this.blockSentences.push(...said);
    }
  }

  /** The approval in force now, if any (expires it after 20 s). */
  live(): LiveApproval | null {
    if (this.current && this.now() - this.current.at >= this.ttlMs) this.end('expired');
    return this.current;
  }

  /** End the live approval; with an id (from the engine), only if it is that approval. */
  end(reason: EndReason, id?: string | null): void {
    const c = this.current;
    if (!c) return;
    if (id != null && c.id != null && id !== c.id) return;
    this.ended.push({ step: c.step, id: c.id, reason, at: this.now() });
    this.current = null;
  }

  /** The engine's power.ended event. */
  onEngineEvent(ev: { event?: string; id?: unknown; reason?: unknown }): void {
    if (ev.event === 'power.ended' && typeof ev.reason === 'string') this.end(ev.reason as EndReason, typeof ev.id === 'string' ? ev.id : null);
  }

  /** The user starts a turn. Talking over the agent is a barge-in, which ends the approval;
   *  a turn after the agent has finished does not. */
  userTurn(bargeIn: boolean): void {
    this.blockSentences = [];
    if (bargeIn) this.end('barge_in');
  }

  check(text: string): Violation | null {
    const a = this.live();
    return findViolation(text, [...(a?.sentences ?? []), ...this.blockSentences]);
  }
}
