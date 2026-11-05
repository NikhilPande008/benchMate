// Companion screen state: the engine's screen.state snapshot (engine/session/repair.py
// screen_state, plus the connection's transcript), with live events applied between snapshots so
// the boot graph, the transcript and the power banner never wait for the next push.

export type Loc = { en: string; hi: string };

export type Fault = {
  id: string;
  name: Loc;
  p: number;
  moved: { evidence: string; measurement: string; outcome: string; delta: number }[];
};

export type Step = {
  measurement: string;
  kind: string;
  label: Loc;
  power_step?: string | null;
  point?: string;
  point_label?: Loc;
  mode?: string;
  probes?: Loc;
  side?: 'top' | 'bottom';
  xy?: [number, number] | null;
};

export type Approval = {
  id: string;
  step: string;
  volts: number | null;
  limit_a: number | null;
  granted_ms: number;
  ttl_s: number;
  speakable: boolean;
  say?: Loc | null;
};

export type Gate = { approved: boolean; step: string; reason?: string; say?: Loc; set?: Record<string, number>; t_ms: number };

export type FieldReading = { value: number | null; unit: string; conf: number };

export type Evidence = {
  id: string;
  kind: string;
  measurement: string | null;
  point: string | null;
  label: Loc | null;
  value: number | null;
  unit: string | null;
  open: boolean;
  mode: string | null;
  outcome: string | null;
  conf: number;
  source: 'camera' | 'spoken';
  confirmed: boolean;
  simulated: boolean;
  legible: boolean;
  frame: string | null;
  pattern: string | null;
  t_ms: number;
};

export type Line = { role: string; text: string; t_ms: number; lang?: string; what?: string; kind?: string };

export type ScreenState = {
  session_id: string;
  state: string;
  symptom: string | null;
  lang: 'en' | 'hi';
  lang_decided: boolean;
  battery: string;
  stopped: string | null;
  degraded: string | null;
  vision: { level: string; reason: string | null };
  pack: { id: string; name: Loc; model_data: boolean; placeholder: boolean } | null;
  readback: string | null;
  faults: Fault[];
  step: Step | null;
  fix: { fault: string; check: string } | null;
  approval: Approval | null;
  gate: Gate | null;
  safe_power?: { volts: number; limit_a: number; v_min: number; v_max: number; limit_max: number };
  supply: { fields: Record<string, FieldReading>; source: string; conf: number; legible: boolean; t_ms: number; simulated: boolean } | null;
  meter: Evidence | null;
  boot: { samples: [number, number][]; pattern: string | null; limit_a: number; live: boolean } | null;
  evidence: Evidence[];
  transcript: Line[];
  scenario?: string;
  camera?: boolean;
};

export type UiEvent = { event: string; [k: string]: unknown };

export const EMPTY: ScreenState = {
  session_id: '',
  state: 'intake',
  symptom: null,
  lang: 'en',
  lang_decided: false,
  battery: 'unknown',
  stopped: null,
  degraded: null,
  vision: { level: 'full', reason: null },
  pack: null,
  readback: null,
  faults: [],
  step: null,
  fix: null,
  approval: null,
  gate: null,
  supply: null,
  meter: null,
  boot: null,
  evidence: [],
  transcript: [],
};

/** Apply one engine UI event. A screen.state replaces everything; a few live events patch it. */
export function apply(s: ScreenState, ev: UiEvent): ScreenState {
  switch (ev.event) {
    case 'screen.state':
      return { ...EMPTY, ...(ev.state as ScreenState) };
    case 'watch.sample': {
      const limit = typeof ev.limit_a === 'number' ? ev.limit_a : (s.boot?.limit_a ?? 1);
      const prev = s.boot?.live ? s.boot.samples : [];
      return { ...s, boot: { samples: [...prev, [Number(ev.t_s), Number(ev.current_a)]], pattern: null, limit_a: limit, live: true } };
    }
    case 'transcript':
      return { ...s, transcript: [...s.transcript.slice(-59), { role: String(ev.role), text: String(ev.text), lang: String(ev.lang ?? ''), t_ms: Date.now() }] };
    case 'interruption':
      return { ...s, transcript: [...s.transcript.slice(-59), { role: 'event', text: 'interrupted', what: String(ev.what ?? ''), t_ms: Date.now() }] };
    case 'power.ended':
      return s.approval && (s.approval.id === ev.id || ev.id == null) ? { ...s, approval: null } : s;
    case 'session.lang':
      return ev.lang === 'hi' || ev.lang === 'en' ? { ...s, lang: ev.lang, lang_decided: Boolean(ev.decided) } : s;
    case 'vision.level':
      return { ...s, vision: { level: String(ev.level), reason: (ev.reason as string | null) ?? null } };
    case 'safety.stop':
      return { ...s, stopped: String(ev.reason ?? 'stopped'), state: 'stopped', approval: null };
    default:
      return s;
  }
}

/** Seconds an approval has left (0 when expired). */
export function approvalLeft(a: Approval, now = Date.now()): number {
  return Math.max(0, Math.ceil((a.granted_ms + a.ttl_s * 1000 - now) / 1000));
}

export type Tone = 'ok' | 'warn' | 'bad' | 'info';

// Status colours always come with a word (PRODUCT.md §10).
const OUTCOMES: Record<string, [string, Tone]> = {
  normal: ['NORMAL', 'ok'],
  short: ['SHORT', 'bad'],
  low: ['LOW', 'warn'],
  high: ['HIGH', 'warn'],
  open: ['OPEN', 'bad'],
  ok: ['OK', 'ok'],
  faulty: ['FAULTY', 'bad'],
  none: ['NOTHING HOT', 'ok'],
  cap_hot: ['CAPACITOR HOT', 'bad'],
  ic_hot: ['IC HOT', 'bad'],
  NORMAL_BOOT: ['NORMAL BOOT', 'ok'],
  SHORT_IMMEDIATE: ['SHORT AT CONNECT', 'bad'],
  NO_CURRENT: ['NO CURRENT', 'bad'],
  RISE_THEN_DROP: ['RISES THEN DROPS', 'warn'],
  AT_LIMIT: ['AT LIMIT', 'bad'],
};

export function outcomeWord(o: string | null | undefined): [string, Tone] {
  if (!o) return ['—', 'info'];
  return OUTCOMES[o] ?? [o.replace(/_/g, ' ').toUpperCase(), 'info'];
}

export function fmtValue(value: number | null, unit: string | null, open = false): string {
  if (open) return 'OL';
  if (value === null || value === undefined) return '—';
  const u = unit === 'diode_V' ? 'V' : unit === 'ohm' ? 'Ω' : (unit ?? '');
  const abs = Math.abs(value);
  const text = unit === 'ohm' && abs >= 1000 ? `${(value / 1000).toFixed(abs >= 1e5 ? 0 : 2)} k` : abs < 10 ? value.toFixed(3) : value.toFixed(2);
  return `${text} ${u}`.trim();
}

export function pct(conf: number): string {
  return `${Math.round(conf * 100)}%`;
}

export const loc = (x: Loc | null | undefined, lang: 'en' | 'hi'): string => (x ? x[lang] || x.en : '');
