// WebSocket to the engine: relays tool calls, ships telemetry spans and transcripts, receives UI
// events and short engine notes for the voice session, and reports barge-ins.

export type UiEvent = { type: 'ui'; event: string; [key: string]: unknown };

/** A short engine message for the voice session (never an instructions update, CLAUDE.md rule 7). */
export type EngineNote = { type: 'inject'; urgent: boolean; text: string };

export type SpanMsg = { name: string; t_ms: number; turn: number; attrs?: Record<string, unknown> };

export type TranscriptMsg = {
  role: 'user' | 'assistant';
  text: string;
  t_ms: number;
  turn: number;
  item_id?: string;
  response_id?: string;
  phase?: string;
  languages?: string[];
};

type Hello = { session_id: string; run_id?: string | null; dummy_delay_s?: number | null; scenario?: string | null };

export class EngineSocket {
  private ws: WebSocket | null = null;
  private ready = false;
  private pending = new Map<string, (output: string) => void>();
  private spanBuffer: SpanMsg[] = [];
  private outbox: unknown[] = [];
  private flushTimer: number | null = null;
  /** Fake bench scenario of this connection (from `ready`, then `dev.scenario` events). */
  scenario: string | null = null;
  /** Set once the voice session exists; notes that arrive earlier wait here. */
  onNote: ((note: EngineNote) => void) | null = null;
  private notes: EngineNote[] = [];

  constructor(private onUi: (ev: UiEvent) => void = () => {}) {}

  setNoteHandler(handler: (note: EngineNote) => void): void {
    this.onNote = handler;
    for (const n of this.notes.splice(0)) handler(n);
  }

  connect(hello: Hello): Promise<void> {
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
    const ws = new WebSocket(url);
    this.ws = ws;
    return new Promise((resolve, reject) => {
      ws.onopen = () => ws.send(JSON.stringify({ type: 'hello', ...hello }));
      ws.onerror = () => reject(new Error('engine WebSocket error'));
      ws.onclose = () => {
        this.ready = false;
        for (const resolveCall of this.pending.values()) resolveCall('{"error":"engine_disconnected"}');
        this.pending.clear();
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data as string);
        if (msg.type === 'ready') {
          this.ready = true;
          this.scenario = msg.scenario ?? null;
          for (const m of this.outbox.splice(0)) ws.send(JSON.stringify(m));
          this.flushTimer = window.setInterval(() => this.flush(), 250);
          this.flush();
          resolve();
        } else if (msg.type === 'tool_result') {
          this.pending.get(msg.call_id)?.(msg.output);
          this.pending.delete(msg.call_id);
        } else if (msg.type === 'ui') {
          if (msg.event === 'dev.scenario') this.scenario = msg.scenario;
          this.onUi(msg as UiEvent);
        } else if (msg.type === 'inject') {
          if (this.onNote) this.onNote(msg as EngineNote);
          else this.notes.push(msg as EngineNote);
        } else if (msg.type === 'error') {
          console.warn('engine:', msg.detail);
        }
      };
    });
  }

  /** Send a tool call to the engine; resolves with its one-line JSON output. */
  callTool(callId: string, name: string, args: unknown, turn: number): Promise<string> {
    return new Promise((resolve) => {
      if (!this.ready || !this.ws) {
        resolve('{"error":"engine_disconnected"}');
        return;
      }
      this.pending.set(callId, resolve);
      this.ws.send(JSON.stringify({ type: 'tool_call', call_id: callId, name, args, turn }));
    });
  }

  /** Spans are batched; anything recorded before the socket is ready is kept and sent later. */
  span(s: SpanMsg): void {
    this.spanBuffer.push(s);
  }

  transcript(t: TranscriptMsg): void {
    this.send({ type: 'transcript', ...t });
  }

  /** The user spoke over the agent: the engine cancels any pending power approval. */
  bargeIn(): void {
    this.send({ type: 'event', name: 'barge_in' });
  }

  /** The output guardrail stopped a power instruction that had no gate approval (rule 2 backstop). */
  safetyViolation(kind: string, text: string, itemId?: string): void {
    this.send({ type: 'event', name: 'safety_violation', kind, text: text.slice(0, 300), item_id: itemId });
  }

  /** The screen's reply-language toggle: sets the session language (the engine tells the model). */
  setLanguage(lang: 'hi' | 'en'): void {
    this.send({ type: 'lang', lang });
  }

  /** Dev controls: switch the fake bench scenario. */
  devScenario(scenario: string): void {
    this.send({ type: 'dev', scenario });
  }

  private send(msg: unknown): void {
    if (this.ready && this.ws) this.ws.send(JSON.stringify(msg));
    else this.outbox.push(msg);
  }

  flush(): void {
    if (!this.spanBuffer.length || !this.ready || !this.ws) return;
    while (this.spanBuffer.length) {
      this.ws.send(JSON.stringify({ type: 'spans', spans: this.spanBuffer.splice(0, 200) }));
    }
  }

  close(): void {
    this.flush();
    if (this.flushTimer !== null) window.clearInterval(this.flushTimer);
    this.ws?.close();
    this.ws = null;
    this.ready = false;
  }
}
