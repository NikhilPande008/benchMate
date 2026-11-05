// Companion screen v1 (PRODUCT.md §10): voice is the input; the screen shows where to probe and
// why the agent believes what it says. Read at a glance from about a metre; not operated during a
// diagnosis. Five zones: board view, supply and meter, fault ledger, evidence log, conversation;
// full-width banners for power steps, safety and degraded camera reading.

import { useEffect, useMemo, useState, type ReactNode } from 'react';

import { Calibration } from './Calibration';
import {
  approvalLeft,
  fmtValue,
  loc,
  outcomeWord,
  pct,
  type Evidence,
  type Fault,
  type Line,
  type ScreenState,
  type Tone,
} from './state';

type Lang = 'en' | 'hi';

export type ReplayInfo = { source: string; t: number; total: number; done: boolean; speed: number };

const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

export type ScreenProps = {
  replay?: ReplayInfo | null;
  state: ScreenState;
  sessionId: string;
  demo: boolean;
  connected: boolean;
  onLanguage: (lang: Lang) => void;
  latencies?: { turn: number; ms: number }[];
  devControls?: ReactNode;
  pttHint?: ReactNode;
};

const STEP_WORDS: Record<string, string> = {
  connect_boot_cable: 'Connect the boot cable',
  press_power_key: 'Press the power key',
  connect_port_supply: 'Connect the supply to the charging port',
};

function Status({ tone, children, testid }: { tone: Tone; children: ReactNode; testid?: string }) {
  return (
    <span className={`status-word ${tone}`} data-testid={testid}>
      {children}
    </span>
  );
}

function useTick(ms: number, on = true): number {
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!on) return;
    const t = window.setInterval(() => setN((x) => x + 1), ms);
    return () => window.clearInterval(t);
  }, [ms, on]);
  return n;
}

// --- banners --------------------------------------------------------------------------------------

function Banners({ s }: { s: ScreenState }) {
  useTick(500, Boolean(s.approval));
  const lang = s.lang;
  const out: ReactNode[] = [];
  if (s.stopped) {
    out.push(
      <div key="stop" className="banner bad" data-testid="banner-stop" role="alert">
        <b>SAFETY STOP</b> {s.stopped}. Disconnect everything.
      </div>,
    );
  }
  if (s.approval) {
    const left = approvalLeft(s.approval);
    out.push(
      <div key="power" className="banner power ok" data-testid="banner-power" role="status">
        <b>POWER STEP APPROVED</b>
        <span className="say">{loc(s.approval.say, lang) || STEP_WORDS[s.approval.step]}</span>
        <span className="values">
          supply <b>{s.approval.volts?.toFixed(2)} V</b> · limit <b>{s.approval.limit_a?.toFixed(2)} A</b> (camera)
        </span>
        <span className="left" data-testid="power-left">
          valid {left} s
        </span>
      </div>,
    );
  } else if (s.step?.power_step && s.state === 'probing') {
    const g = s.gate && s.gate.step === s.step.power_step && !s.gate.approved ? s.gate : null;
    out.push(
      <div key="pending" className="banner power warn" data-testid="banner-power-pending" role="status">
        <b>POWER STEP PENDING</b>
        <span className="say">{STEP_WORDS[s.step.power_step] ?? s.step.power_step}</span>
        <span className="values">
          the camera must read supply <b>{s.safe_power?.volts.toFixed(1)} V</b> and limit <b>{s.safe_power?.limit_a.toFixed(1)} A</b>
        </span>
        {g && (
          <span className="blocked" data-testid="power-blocked">
            BLOCKED: {loc(g.say, lang) || g.reason}
          </span>
        )}
      </div>,
    );
  }
  if (s.vision.level === 'reader_b_only') {
    out.push(
      <div key="vision" className="banner warn" data-testid="banner-degraded">
        <b>CAMERA READING LIMITED</b> local reader only: every value is read back for you to confirm; no power steps.
      </div>,
    );
  } else if (s.vision.level === 'spoken_only' || s.degraded === 'camera_unavailable') {
    out.push(
      <div key="vision" className="banner bad" data-testid="banner-degraded">
        <b>CAMERA UNAVAILABLE</b> {s.vision.reason === 'camera_moved' ? 'the camera moved: recalibrate.' : 'read the values aloud.'}
      </div>,
    );
  }
  if (s.record) {
    out.push(
      <div key="record" className="banner info" data-testid="banner-record">
        <b>REPAIR RECORD</b> {s.record}
        <a href={`/records/${s.record}/report.pdf`} target="_blank" rel="noreferrer" data-testid="record-report">
          Repair report (PDF)
        </a>
        <a href={`/records/${s.record}/assessment.pdf`} target="_blank" rel="noreferrer" data-testid="record-assessment">
          Trainee sheet (PDF)
        </a>
        <a href={`/records/${s.record}/report.html`} target="_blank" rel="noreferrer">
          HTML
        </a>
      </div>,
    );
  }
  if (s.readback) {
    out.push(
      <div key="rb" className="banner info" data-testid="banner-readback">
        <b>CONFIRM</b> {s.readback}: say “haan” or “yes”.
      </div>,
    );
  }
  return out.length ? <div className="banners">{out}</div> : null;
}

// --- board view ---------------------------------------------------------------------------------------

type BoardPoint = { point: string | null; side?: string; ref?: [number, number]; ref_size: [number, number]; live?: [number, number] | null; live_size?: [number, number]; aligned?: boolean };

/** The next point: a ring sized to the image, so it reads from a metre whatever the scale. */
function Marker({ x, y, label, width }: { x: number; y: number; label: string; width: number }) {
  const r = width * 0.055;
  const sw = width * 0.012;
  return (
    <g className="marker">
      <circle cx={x} cy={y} r={r} className="halo" strokeWidth={sw * 2.2} />
      <circle cx={x} cy={y} r={r} strokeWidth={sw} />
      <circle cx={x} cy={y} r={sw * 1.1} className="dot" />
      <text x={x + r * 1.25} y={y + r * 0.35} fontSize={width * 0.05} strokeWidth={width * 0.008}>
        {label}
      </text>
    </g>
  );
}

function BoardView({ s, sessionId }: { s: ScreenState; sessionId: string }) {
  const lang = s.lang;
  const tick = useTick(700, Boolean(s.pack));
  const [bp, setBp] = useState<BoardPoint | null>(null);
  const point = s.step?.point ?? null;
  useEffect(() => {
    if (!s.pack || !point) {
      setBp(null);
      return;
    }
    let alive = true;
    fetch(`/screen/board-point?session=${encodeURIComponent(sessionId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => alive && setBp(j))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [s.pack, point, sessionId, Math.floor(tick / 2)]);
  const side = s.step?.side ?? 'top';
  const step = s.step;
  return (
    <section className="zone board" data-testid="board">
      <h2>Board</h2>
      {step ? (
        <p className="next" data-testid="next-step">
          <span className="label">Next</span> {step.point_label ? loc(step.point_label, lang) : loc(step.label, lang)}
          {step.mode && <span className="mode">{step.mode} mode</span>}
          {step.probes && <span className="probes">{loc(step.probes, lang)}</span>}
        </p>
      ) : (
        <p className="next muted">{s.state === 'fix' ? 'Fix proposed: see the ledger.' : s.pack ? 'Waiting for the next step.' : 'Start a repair by voice.'}</p>
      )}
      {s.pack ? (
        <div className="board-images">
          <figure>
            <div className="img-wrap">
              <img src={`/screen/frame/microscope.jpg?session=${encodeURIComponent(sessionId)}&n=${tick}`} alt="live microscope" data-testid="microscope" />
              {bp?.live && bp.live_size && (
                <svg viewBox={`0 0 ${bp.live_size[0]} ${bp.live_size[1]}`} className="overlay" data-testid="live-marker">
                  <Marker x={bp.live[0]} y={bp.live[1]} label={bp.point ?? ''} width={bp.live_size[0]} />
                </svg>
              )}
            </div>
            <figcaption>
              Live microscope (fake camera){bp && bp.point && !bp.live && ' · board not aligned: point shown on the reference only'}
            </figcaption>
          </figure>
          <figure>
            <div className="img-wrap">
              <img src={`/screen/board/${encodeURIComponent(sessionId)}/${side}.jpg`} alt="reference board" data-testid="reference" />
              {step?.xy && (
                <svg viewBox={`0 0 ${bp?.ref_size?.[0] ?? 640} ${bp?.ref_size?.[1] ?? 1040}`} className="overlay" data-testid="ref-marker">
                  <Marker x={step.xy[0]} y={step.xy[1]} label={step.point ?? ''} width={bp?.ref_size?.[0] ?? 640} />
                </svg>
              )}
            </div>
            <figcaption>Reference board, {side} (synthetic stand-in)</figcaption>
          </figure>
        </div>
      ) : null}
    </section>
  );
}

// --- supply and meter -----------------------------------------------------------------------------------

function BootGraph({ boot }: { boot: NonNullable<ScreenState['boot']> }) {
  const W = 400;
  const H = 150;
  const tMax = Math.max(4, ...boot.samples.map((p) => p[0]));
  const iMax = Math.max(boot.limit_a * 1.1, ...boot.samples.map((p) => p[1]));
  const x = (t: number) => 30 + (t / tMax) * (W - 40);
  const y = (i: number) => H - 20 - (i / iMax) * (H - 30);
  const pts = boot.samples.map(([t, i]) => `${x(t).toFixed(1)},${y(i).toFixed(1)}`).join(' ');
  const [word, tone] = outcomeWord(boot.pattern);
  return (
    <div className="boot" data-testid="boot">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="boot current">
        <line x1={30} x2={W - 10} y1={y(boot.limit_a)} y2={y(boot.limit_a)} className="limit" />
        <text x={W - 12} y={y(boot.limit_a) - 4} className="axis" textAnchor="end">
          limit {boot.limit_a.toFixed(2)} A
        </text>
        <line x1={30} x2={W - 10} y1={H - 20} y2={H - 20} className="axisline" />
        <text x={30} y={H - 4} className="axis">
          0 s
        </text>
        <text x={W - 10} y={H - 4} className="axis" textAnchor="end">
          {tMax.toFixed(1)} s
        </text>
        <polyline points={pts} className="trace" data-testid="boot-trace" />
      </svg>
      <p>
        Boot current {boot.live ? <Status tone="info">WATCHING</Status> : <Status tone={tone} testid="boot-pattern">{word}</Status>}
      </p>
    </div>
  );
}

function SourceWord({ source }: { source: string }) {
  return (
    <span className={`source ${source}`}>
      {source === 'camera' ? (
        <svg viewBox="0 0 24 24" className="icon" aria-hidden="true">
          <path d="M4 7h3l2-2h6l2 2h3v12H4z" fill="none" stroke="currentColor" strokeWidth="2" />
          <circle cx="12" cy="13" r="3.5" fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" className="icon" aria-hidden="true">
          <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="2" />
          <path d="M6 11a6 6 0 0 0 12 0M12 17v4" fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
      )}
      {source === 'camera' ? 'Camera' : 'Spoken'}
    </span>
  );
}

function Readings({ s }: { s: ScreenState }) {
  const sup = s.supply;
  const met = s.meter;
  const sp = s.safe_power;
  const v = sup?.fields.set_voltage;
  const lim = sup?.fields.current_limit;
  const live = sup?.fields.live_current;
  const vOk = v?.value != null && sp ? v.value >= sp.v_min && v.value <= sp.v_max : null;
  const [mword, mtone] = outcomeWord(met?.outcome);
  return (
    <section className="zone readings" data-testid="readings">
      <h2>Supply and meter</h2>
      <div className="supply" data-testid="supply">
        {sup ? (
          <>
            <div className="big">
              <span data-testid="supply-volts">{v?.value != null ? v.value.toFixed(2) : '—'}</span>
              <small>V</small>
              <span data-testid="supply-limit">{lim?.value != null ? lim.value.toFixed(2) : '—'}</span>
              <small>A limit</small>
            </div>
            <p className="meta">
              live {live?.value != null ? `${live.value.toFixed(3)} A` : '—'} · <SourceWord source={sup.source} /> · {pct(sup.conf)}{' '}
              {vOk === null ? null : vOk ? <Status tone="ok">IN BAND</Status> : <Status tone="bad">SET {sp?.volts.toFixed(1)} V</Status>}
              {!sup.legible && <Status tone="warn">UNCLEAR</Status>}
            </p>
          </>
        ) : (
          <p className="muted">No supply reading yet.</p>
        )}
      </div>
      <div className="meter" data-testid="meter">
        {met ? (
          <>
            <div className="big">
              <span data-testid="meter-value">{fmtValue(met.value, met.unit, met.open)}</span>
            </div>
            <p className="meta">
              {met.label ? loc(met.label, s.lang) : met.point} · {met.mode} · <SourceWord source={met.source} /> · {pct(met.conf)}{' '}
              <Status tone={mtone} testid="meter-class">{mword}</Status>
            </p>
          </>
        ) : (
          <p className="muted">No meter reading yet.</p>
        )}
      </div>
      {s.boot && s.boot.samples.length > 0 && <BootGraph boot={s.boot} />}
    </section>
  );
}

// --- ledger ---------------------------------------------------------------------------------------------

function Ledger({ s }: { s: ScreenState }) {
  const shown: Fault[] = s.faults.filter((f, i) => i < 5 && (i < 2 || f.p >= 0.01));
  const byId = useMemo(() => Object.fromEntries(s.evidence.map((e) => [e.id, e])), [s.evidence]);
  return (
    <section className="zone ledger" data-testid="ledger">
      <h2>Fault ledger</h2>
      {shown.length === 0 && <p className="muted">Ranks the likely faults once the repair starts.</p>}
      <ol>
        {shown.map((f) => (
          <li key={f.id} data-testid={`fault-${f.id}`}>
            <div className="row">
              <span className="name">{loc(f.name, s.lang)}</span>
              <span className="p">{pct(f.p)}</span>
            </div>
            <div className="bar">
              <div style={{ width: `${Math.max(1, f.p * 100)}%` }} />
            </div>
            {f.moved.length > 0 && (
              <p className="moved">
                {f.moved.map((m) => {
                  const e = byId[m.evidence];
                  const [w] = outcomeWord(m.outcome);
                  return (
                    <span key={m.evidence} className={m.delta > 0 ? 'up' : 'down'}>
                      {e?.label ? loc(e.label, s.lang) : m.measurement} {w} {m.delta > 0 ? '+' : '−'}
                      {Math.round(Math.abs(m.delta) * 100)}
                    </span>
                  );
                })}
              </p>
            )}
          </li>
        ))}
      </ol>
      {s.fix && (
        <p className="fix" data-testid="fix">
          <Status tone="warn">FIX</Status> {loc(s.faults.find((f) => f.id === s.fix?.fault)?.name, s.lang)}; then check {s.fix.check}.
        </p>
      )}
    </section>
  );
}

// --- evidence log ---------------------------------------------------------------------------------------

function EvidenceLog({ s, onOpen }: { s: ScreenState; onOpen: (e: Evidence) => void }) {
  const items = [...s.evidence].reverse();
  return (
    <section className="zone evidence" data-testid="evidence">
      <h2>Evidence</h2>
      {items.length === 0 && <p className="muted">Every reading appears here with its source and frame.</p>}
      <ul>
        {items.map((e) => {
          const [w, tone] = outcomeWord(e.kind === 'boot_trace' ? e.pattern : e.outcome);
          const what = e.kind === 'supply' ? 'Supply' : e.label ? loc(e.label, s.lang) : (e.point ?? e.measurement ?? e.kind);
          return (
            <li key={e.id} data-testid="evidence-item">
              {e.frame ? (
                <button className="thumb" onClick={() => onOpen(e)} title="Open the frame" data-testid="thumb">
                  <img src={e.frame} alt="frame" />
                </button>
              ) : (
                <span className="thumb none">no frame</span>
              )}
              <div>
                <p className="what">
                  {what} <b>{e.kind === 'supply' ? '' : e.kind === 'boot_trace' ? '' : fmtValue(e.value, e.unit, e.open)}</b>{' '}
                  {(e.outcome || e.pattern) && <Status tone={tone}>{w}</Status>}
                </p>
                <p className="meta">
                  <SourceWord source={e.source} /> · {pct(e.conf)}
                  {e.confirmed && ' · confirmed'}
                  {!e.legible && ' · unclear'}
                  {e.simulated && ' · simulated'}
                </p>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// --- conversation -------------------------------------------------------------------------------------------

const LANG_TAG: Record<string, string> = { hi: 'HI', hinglish: 'HINGLISH', en: 'EN', unknown: '?' };

function Conversation({ lines }: { lines: Line[] }) {
  return (
    <section className="zone conversation" data-testid="conversation">
      <h2>Conversation</h2>
      {lines.length === 0 && <p className="muted">The transcript appears here.</p>}
      <ol>
        {lines.slice(-30).map((l, i) =>
          l.role === 'event' ? (
            <li key={i} className="marker-line" data-testid="interruption">
              — {l.what === 'guardrail' ? 'power instruction stopped' : l.text} —
            </li>
          ) : (
            <li key={i} className={l.role} data-testid="line">
              <span className="who">{l.role === 'user' ? 'You' : 'Benchmate'}</span> {l.text}{' '}
              {l.role === 'user' && l.lang && <span className="tag" data-testid="lang-tag">{LANG_TAG[l.lang] ?? l.lang}</span>}
            </li>
          ),
        )}
      </ol>
    </section>
  );
}

// --- latency dashboard (dev only) ------------------------------------------------------------------------------

function pctile(xs: number[], q: number): number {
  const s = [...xs].sort((a, b) => a - b);
  return s.length ? s[Math.min(s.length - 1, Math.floor(q * s.length))] : 0;
}

function Latency({ items }: { items: { turn: number; ms: number }[] }) {
  const ms = items.map((x) => x.ms);
  return (
    <section className="zone latency" data-testid="latency">
      <h2>Latency: speech end → first audio</h2>
      {ms.length ? (
        <p>
          p50 <b>{Math.round(pctile(ms, 0.5))} ms</b> · p95 <b>{Math.round(pctile(ms, 0.95))} ms</b> · {ms.length} turns
        </p>
      ) : (
        <p className="muted">No voice turns yet.</p>
      )}
    </section>
  );
}

// --- the screen ---------------------------------------------------------------------------------------------

function useTheme(): [string, (t: string) => void] {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem('benchmate.theme') ?? 'auto';
    } catch {
      return 'auto';
    }
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem('benchmate.theme', theme);
    } catch {
      /* private mode */
    }
  }, [theme]);
  return [theme, setTheme];
}

export function Screen({ state: s, sessionId, demo, connected, onLanguage, latencies = [], devControls, pttHint, replay }: ScreenProps) {
  const [theme, setTheme] = useTheme();
  const [open, setOpen] = useState<Evidence | null>(null);
  const [calibrating, setCalibrating] = useState(false);
  const stateWord: Record<string, [string, Tone]> = {
    intake: ['INTAKE', 'info'],
    probing: ['PROBING', 'info'],
    fix: ['FIX', 'warn'],
    closed: ['CLOSED', 'ok'],
    unresolved: ['UNRESOLVED', 'warn'],
    stopped: ['STOPPED', 'bad'],
  };
  const [sw, st] = stateWord[s.state] ?? [s.state.toUpperCase(), 'info'];
  return (
    <div className={`screen${demo ? ' demo' : ''}`} data-testid="screen">
      <header className="screen-head">
        <h1>Benchmate</h1>
        <span className="pack">{s.pack ? loc(s.pack.name, s.lang) : 'no repair yet'}</span>
        <Status tone={st} testid="session-state">{sw}</Status>
        {!connected && <Status tone="bad">ENGINE DISCONNECTED</Status>}
        {pttHint}
        <span className="spacer" />
        <span className="toggle" data-testid="language">
          Reply language
          {(['hi', 'en'] as const).map((l) => (
            <button key={l} className={s.lang === l ? 'on' : ''} aria-pressed={s.lang === l} onClick={() => onLanguage(l)} disabled={!connected}>
              {l === 'hi' ? 'Hinglish' : 'English'}
            </button>
          ))}
        </span>
        <span className="toggle" data-testid="theme">
          {(['auto', 'light', 'dark'] as const).map((t) => (
            <button key={t} className={theme === t ? 'on' : ''} aria-pressed={theme === t} onClick={() => setTheme(t)}>
              {t}
            </button>
          ))}
        </span>
        {!demo && s.camera && (
          <button className="dev" onClick={() => setCalibrating(true)} data-testid="open-calibration">
            Calibrate
          </button>
        )}
      </header>
      {!demo && devControls}
      {replay && (
        <div className="banner info replay" data-testid="banner-replay">
          <b>REPLAY</b> of session {replay.source} · {replay.done ? 'finished' : `${clock(replay.t)} / ${clock(replay.total)}`} · speed {replay.speed}× · no voice, no API
        </div>
      )}
      <Banners s={s} />
      <div className="grid">
        <BoardView s={s} sessionId={sessionId} />
        <Readings s={s} />
        <Ledger s={s} />
        <EvidenceLog s={s} onOpen={setOpen} />
        <Conversation lines={s.transcript} />
        {!demo && <Latency items={latencies} />}
      </div>
      {open && (
        <div className="modal" role="dialog" data-testid="frame-modal" onClick={() => setOpen(null)}>
          <figure onClick={(e) => e.stopPropagation()}>
            <img src={open.frame ?? ''} alt="source frame" data-testid="frame-full" />
            <figcaption>
              {open.label ? loc(open.label, s.lang) : open.kind} · {fmtValue(open.value, open.unit, open.open)} · {pct(open.conf)} · {new Date(open.t_ms).toLocaleTimeString()}
            </figcaption>
            <button onClick={() => setOpen(null)}>Close</button>
          </figure>
        </div>
      )}
      {calibrating && !demo && <Calibration sessionId={sessionId} onClose={() => setCalibrating(false)} />}
    </div>
  );
}
