// Session rotation and reconnect (BUILD_GUIDE Latency engineering 12; CLAUDE.md decided settings).
//
// Rotation: after ~25 turns, ~30 minutes or a large context, at a natural pause (nothing playing,
// no reply or tool in flight, the talk key up, quiet for a moment, and the engine agrees: no power
// approval live, no read-back waiting, no boot watch), the next voice session is pre-opened in the
// background, muted and inactive, seeded with the engine's summary, and swapped in. The user
// hears nothing. If the user starts talking before the swap, the standby waits for the next pause.
//
// Reconnect: when the active connection drops, the standby (if ready) takes over at once;
// otherwise a new session is opened with backoff. Either way it is seeded with the summary and
// says it's back.
//
// The transport is an interface so all of this runs against a fake in node --test; the real one
// wraps an Agents SDK RealtimeSession (voice.ts).

export type RotationConfig = { enabled: boolean; max_turns: number; max_minutes: number; max_context_tokens: number };

export type LiveView = { audioPlaying: boolean; responsesInFlight: number; toolsPending: number; pttDown: boolean; lastActivity: number };

export interface VoiceSession {
  readonly id: string;
  /** Start hearing the mic and playing audio; take the engine's notes. */
  activate(): void;
  /** Stop hearing and playing (a standby, or the old session during a swap). */
  deactivate(): void;
  close(): void;
  /** A system item into the conversation; `respond` asks for a reply now. */
  inject(text: string, respond: boolean): void;
  onDrop(cb: (reason: string) => void): void;
  live(): LiveView;
  /** Input tokens of the last reply: the context the model is carrying. */
  contextTokens(): number;
}

export type Summary = { text: string; pause_ok: boolean; why?: string | null };

export type ManagerDeps = {
  open: () => Promise<VoiceSession>; // mint a client secret for this repair session and connect, inactive
  summary: () => Promise<Summary>;
  report: (event: string, attrs: Record<string, unknown>) => void; // spans + engine event
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  random?: () => number;
};

export const QUIET_MS = 1500;
export const BACK_NOTE = '[engine] The voice connection dropped and is back. Say only: "I\'m back." Then wait for the user.';

export function rotationDue(cfg: RotationConfig, turns: number, startedAt: number, contextTokens: number, now: number): string | null {
  if (!cfg.enabled) return null;
  if (turns >= cfg.max_turns) return 'turns';
  if (now - startedAt >= cfg.max_minutes * 60_000) return 'time';
  if (contextTokens >= cfg.max_context_tokens) return 'tokens';
  return null;
}

export function naturalPause(v: LiveView, now: number, quietMs = QUIET_MS): boolean {
  return !v.audioPlaying && v.responsesInFlight === 0 && v.toolsPending === 0 && !v.pttDown && now - v.lastActivity >= quietMs;
}

/** Exponential backoff with jitter: 0.5, 1, 2, 4, 8, 8 … seconds. */
export class Backoff {
  private n = 0;
  private random: () => number;
  readonly baseMs: number;
  readonly maxMs: number;
  readonly jitter: number;
  constructor(random: () => number = Math.random, baseMs = 500, maxMs = 8000, jitter = 0.2) {
    this.random = random;
    this.baseMs = baseMs;
    this.maxMs = maxMs;
    this.jitter = jitter;
  }
  next(): number {
    const d = Math.min(this.maxMs, this.baseMs * 2 ** this.n);
    this.n += 1;
    return Math.round(d * (1 - this.jitter + 2 * this.jitter * this.random()));
  }
  reset(): void {
    this.n = 0;
  }
  get attempts(): number {
    return this.n;
  }
}

export class SessionManager {
  active: VoiceSession | null = null;
  standby: VoiceSession | null = null;
  turns = 0;
  startedAt: number;
  rotations = 0;
  reconnects = 0;
  busy: 'idle' | 'preparing' | 'swapping' | 'reconnecting' = 'idle';
  maxAttempts = 8;
  private now: () => number;
  private sleep: (ms: number) => Promise<void>;
  private backoff: Backoff;
  private preparing: Promise<VoiceSession | null> | null = null;
  private closed = false;
  /** Called whenever the active session changes (the page re-points its handle). */
  onSwap: (s: VoiceSession) => void = () => {};

  private cfg: RotationConfig;
  private deps: ManagerDeps;

  constructor(cfg: RotationConfig, deps: ManagerDeps) {
    this.cfg = cfg;
    this.deps = deps;
    this.now = deps.now ?? (() => Date.now());
    this.sleep = deps.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.backoff = new Backoff(deps.random);
    this.startedAt = this.now();
  }

  /** The first session, already connected by the page. */
  adopt(s: VoiceSession): void {
    this.active = s;
    this.watch(s);
    this.startedAt = this.now();
    this.turns = 0;
  }

  private watch(s: VoiceSession): void {
    s.onDrop((reason) => {
      if (s === this.active && !this.closed) void this.reconnect(reason);
      else if (s === this.standby) this.standby = null;
    });
  }

  /** A turn finished on the active session. */
  turnDone(): void {
    this.turns += 1;
  }

  due(): string | null {
    return rotationDue(this.cfg, this.turns, this.startedAt, this.active?.contextTokens() ?? 0, this.now());
  }

  /** Called periodically (about once a second): rotate if due and at a natural pause. */
  async tick(): Promise<string | null> {
    const reason = this.due();
    if (!reason || this.busy !== 'idle' || !this.active || this.closed) return null;
    if (!this.standby) {
      void this.prepare(); // in the background; the swap happens at a later tick
      return null;
    }
    if (!naturalPause(this.active.live(), this.now())) return null;
    const summary = await this.deps.summary();
    if (!summary.pause_ok) return null; // the engine says not now (approval live, read-back, watch)
    if (!naturalPause(this.active.live(), this.now())) return null; // the user started while we asked
    await this.swap(this.standby, summary.text, reason, false);
    return reason;
  }

  /** Pre-open the next session (muted, inactive). */
  prepare(): Promise<VoiceSession | null> {
    if (this.standby) return Promise.resolve(this.standby);
    if (this.preparing) return this.preparing;
    this.busy = 'preparing';
    this.preparing = this.deps
      .open()
      .then((s) => {
        if (this.closed) {
          s.close();
          return null;
        }
        this.standby = s;
        this.watch(s);
        return s;
      })
      .catch((err) => {
        this.deps.report('standby_failed', { error: String(err?.message ?? err) });
        return null;
      })
      .finally(() => {
        this.preparing = null;
        if (this.busy === 'preparing') this.busy = 'idle';
      });
    return this.preparing;
  }

  private async swap(next: VoiceSession, summary: string, reason: string, sayBack: boolean): Promise<void> {
    const t0 = this.now();
    this.busy = 'swapping';
    const old = this.active;
    next.inject(`[engine] ${summary}`, false);
    if (sayBack) next.inject(BACK_NOTE, true);
    old?.deactivate();
    next.activate();
    this.active = next;
    this.standby = null;
    old?.close();
    this.turns = 0;
    this.startedAt = this.now();
    if (reason === 'reconnect') this.reconnects += 1;
    else this.rotations += 1;
    this.busy = 'idle';
    this.deps.report('rotated', { reason, ms: Math.round(this.now() - t0), from: old?.id ?? null, to: next.id });
    this.onSwap(next);
  }

  /** The active connection dropped: the standby takes over, or a new session with backoff. */
  async reconnect(reason: string): Promise<boolean> {
    if (this.busy === 'reconnecting' || this.closed) return false;
    this.busy = 'reconnecting';
    this.deps.report('connection_dropped', { reason });
    this.active?.close();
    this.active = null;
    this.backoff.reset();
    let next = this.standby;
    this.standby = null;
    while (!next && this.backoff.attempts < this.maxAttempts && !this.closed) {
      try {
        next = await this.deps.open();
      } catch (err) {
        const wait = this.backoff.next();
        this.deps.report('reconnect_failed', { attempt: this.backoff.attempts, wait_ms: wait, error: String((err as Error)?.message ?? err) });
        await this.sleep(wait);
      }
    }
    if (!next || this.closed) {
      this.busy = 'idle';
      this.deps.report('reconnect_gave_up', { attempts: this.backoff.attempts });
      return false;
    }
    this.watch(next);
    let summary: Summary;
    try {
      summary = await this.deps.summary();
    } catch {
      summary = { text: 'Session summary unavailable: call next_step before the next instruction.', pause_ok: true };
    }
    await this.swap(next, summary.text, 'reconnect', true);
    return true;
  }

  close(): void {
    this.closed = true;
    this.active?.close();
    this.standby?.close();
    this.active = this.standby = null;
  }
}
