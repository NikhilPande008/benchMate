// Voice session: Agents SDK RealtimeSession over WebRTC, configured entirely by the engine.
// The browser never sees the OpenAI API key, only a short-lived client secret (ek_...).
//
// Connect early: mic, client secret and (for local preambles) the clip player start in parallel
// as soon as the page loads, and the session stays open for reuse. Every stage is a span.

import {
  OpenAIRealtimeWebRTC,
  RealtimeAgent,
  RealtimeSession,
  tool,
  type RealtimeSessionConfig,
  type TransportEvent,
} from '@openai/agents-realtime';

import { filterDataChannel } from './channelFilter';
import { PowerApprovals } from './powerGuard';
import { SessionManager, type RotationConfig, type VoiceSession } from './rotation';
import { EngineSocket, type EngineNote, type UiEvent } from './engine';
import { clipLangFor, LocalPreamble, type PreambleLang, type PreamblePath } from './preamble';
import { nowMs, Spans, watchAudioOnset } from './telemetry';

type ToolSpec = { type: 'function'; name: string; description: string; parameters: Record<string, unknown> };
type TurnDetection = Record<string, unknown> | null;

export type VoiceConfig = {
  model: string;
  instructions: string;
  prompt: string;
  prompt_version: string;
  preamble: 'model' | 'local';
  voice: string;
  turn_mode: string;
  turn_detection: TurnDetection;
  turn_modes: Record<string, TurnDetection>;
  reasoning: { effort: 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' };
  truncation: Record<string, unknown>;
  rotation?: RotationConfig;
  max_output_tokens: number | null;
  transcription: Record<string, unknown>;
  tools: ToolSpec[];
  /** Browser-side SDK timeout per tool: a little longer than the engine's own timeout. */
  tool_timeouts_ms?: Record<string, number>;
  /** Dev controls (null when the engine's dev endpoints are off). */
  dev?: { scenarios: string[]; scenario: string } | null;
};

type ClientSecretResponse = {
  session_id: string;
  client_secret: string;
  expires_at: number;
  secret_source: 'pool' | 'fresh';
  mint_ms: number;
  config: VoiceConfig;
};

export type VoiceEvent =
  | { kind: 'status'; text: string }
  | { kind: 'user'; text: string; lang: string }
  | { kind: 'assistant'; text: string; phase?: string }
  | { kind: 'ui'; event: UiEvent }
  | { kind: 'latency'; turn: number; firstAudioMs: number }
  | { kind: 'stage'; name: string; ms: number }
  | { kind: 'turn_mode'; mode: string }
  | { kind: 'ptt'; down: boolean }
  | { kind: 'audio_locked'; locked: boolean }
  | { kind: 'error'; text: string };

export type VoiceOptions = {
  model: string;
  runId?: string | null;
  dummyDelayS?: number | null;
  turnMode?: string | null;
  prompt?: string | null;
  outputCap?: boolean | null;
  usePool?: boolean;
  pttKey?: string;
  /** Fake bench scenario for this connection (dev). */
  scenario?: string | null;
  /** Debug: a span for every incoming server event (deltas excepted). */
  traceEvents?: boolean;
  /** Harness only: an injected stream instead of the microphone. */
  micStream?: MediaStream;
  preamblePath?: PreamblePath;
  onEvent: (ev: VoiceEvent) => void;
};

export type LiveState = {
  responsesInFlight: number;
  audioPlaying: boolean;
  toolsPending: number;
  lastActivity: number;
  lastSpeechEnd: number;
  userLang: string | null;
  /** The engine's session language (two consecutive turns to switch); picks the preamble clip. */
  sessionLang: PreambleLang | null;
  pttDown: boolean;
  /** Input tokens of the last reply: the context the model carries (rotation). */
  contextTokens: number;
};

export type VoiceHandle = {
  sessionId: string;
  model: string;
  config: VoiceConfig;
  spans: Spans;
  state: LiveState;
  turnMode: () => string;
  setTurnMode: (mode: string) => void;
  pttKey: () => string;
  setPttKey: (code: string) => void;
  pttDown: () => void;
  pttUp: () => void;
  clipPlaying: () => boolean;
  scenario: () => string | null;
  setScenario: (name: string) => void;
  setLanguage: (lang: PreambleLang) => void;
  /** Diagnostics: the WebRTC peer connection to OpenAI. */
  peer: () => RTCPeerConnection | null;
  stop: () => void;
};

const HINGLISH = new Set(
  ('hai hain kya ka ki ke ko se mein nahi nahin haan theek thik kitna kitni dikha raha rahi rahe padho padh ' +
    'batao samjhao matlab hota hoti aur ek ruko aage wapas abhi laga lagao diya karo chalo yeh ye toh bhi ' +
    'dekho ji aap kaise kahan farak accha acha shukriya dikhata pe par liye').split(' '),
);

/** Same rule as engine/telemetry/lang.py: script first, then Latin-script Hindi words, then the API's codes. */
export function tagLang(text: string, codes: string[] = []): string {
  if (/[ऀ-ॿ]/.test(text)) return 'hi';
  const words = text.toLowerCase().match(/[a-z']+/g) ?? [];
  const hindi = words.filter((w) => HINGLISH.has(w)).length;
  if (hindi >= 1 && hindi / Math.max(words.length, 1) >= 0.2) return 'hinglish';
  if (codes.includes('hi') && !codes.includes('en')) return 'hi';
  return words.length ? 'en' : 'unknown';
}

type SecretRequest = {
  model: string;
  run_id: string | null;
  turn_mode: string | null;
  prompt: string | null;
  output_cap: boolean | null;
  use_pool: boolean;
  session_id?: string; // a rotation or reconnect: continue this repair session
};

/** Started by the inline script in index.html before the bundle loads (one use only). */
type EarlyStart = {
  t: number;
  body: SecretRequest;
  secret: Promise<ClientSecretResponse>;
  secretAt?: number;
  mic?: Promise<MediaStream>;
  micAt?: number;
  used?: boolean;
};

declare global {
  interface Window {
    __benchmateEarly?: EarlyStart;
  }
}

const secretRequest = (opts: VoiceOptions): SecretRequest => ({
  model: opts.model,
  run_id: opts.runId ?? null,
  turn_mode: opts.turnMode ?? null,
  prompt: opts.prompt ?? null,
  output_cap: opts.outputCap ?? null,
  use_pool: opts.usePool ?? true,
});

async function mintClientSecret(body: SecretRequest): Promise<ClientSecretResponse> {
  const resp = await fetch('/realtime/client-secret', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`client secret: ${resp.status} ${(await resp.json().catch(() => ({}))).detail ?? ''}`);
  return resp.json();
}

/** Map the engine's (Realtime API snake_case) config onto the SDK's session config. */
function sdkConfig(cfg: VoiceConfig): Partial<RealtimeSessionConfig> {
  return {
    outputModalities: ['audio'],
    audio: {
      input: { turnDetection: cfg.turn_detection, transcription: cfg.transcription, noiseReduction: null },
      output: { voice: cfg.voice },
    },
    reasoning: cfg.reasoning,
    // Not SDK config keys: spread into session.update as-is.
    providerData: {
      truncation: cfg.truncation,
      ...(cfg.max_output_tokens !== null ? { max_output_tokens: cfg.max_output_tokens } : {}),
    },
  };
}

// Passed to the model by the SDK when the power guardrail trips (with the offending sentence).
const POWER_POLICY_HINT =
  "Power instructions may be spoken only with the safety gate's exact sentence, in the turn the gate approved it. " +
  'Say one short sentence telling the user not to connect or press anything yet, in the reply language ' +
  `("Ruko, abhi kuch connect mat karo." or "Wait, don't connect anything yet."), then call next_step.`;

const isTyping = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName));

export async function startVoice(opts: VoiceOptions): Promise<VoiceHandle> {
  let onUi: (ev: UiEvent) => void = () => {};
  const engine = new EngineSocket((ev) => {
    onUi(ev);
    opts.onEvent({ kind: 'ui', event: ev });
  });
  const setUiHook = (hook: (ev: UiEvent) => void) => (onUi = hook);
  const spans = new Spans(engine);
  const stage = (name: string, attrs?: Record<string, unknown>, t?: number) => {
    const s = spans.record(name, attrs, t);
    opts.onEvent({ kind: 'stage', name, ms: s.t_ms - performance.timeOrigin });
  };
  stage('page_load', undefined, performance.timeOrigin);
  const body = secretRequest(opts);
  const early = window.__benchmateEarly;
  const useEarly = Boolean(early && !early.used && JSON.stringify(early.body) === JSON.stringify(body));
  if (early && useEarly) {
    early.used = true;
    stage('early_start', undefined, performance.timeOrigin + early.t);
  }
  stage('voice_start', { early: useEarly });
  const at = (ms: number | undefined) => (ms === undefined ? undefined : performance.timeOrigin + ms);

  const micP = (
    opts.micStream
      ? Promise.resolve(opts.micStream)
      : useEarly && early!.mic
        ? early!.mic
        : navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, autoGainControl: false, noiseSuppression: false, channelCount: 1 },
          })
  ).then((m) => {
    stage('mic_granted', { injected: Boolean(opts.micStream) }, useEarly && !opts.micStream ? at(early!.micAt) : undefined);
    return m;
  });
  const secretP = (useEarly ? early!.secret : mintClientSecret(body)).then((r) => {
    stage('secret_fetched', { source: r.secret_source, mint_ms: r.mint_ms }, useEarly ? at(early!.secretAt) : undefined);
    return r;
  });
  try {
    const [mic, secret] = await Promise.all([micP, secretP]);
    let manager: SessionManager | null = null;
    const onTurn = () => manager?.turnDone();
    const first = await connectSession(opts, mic, secret, engine, spans, stage, setUiHook, { standby: false, onTurn });
    const cfg = secret.config;
    const handles = new Map<VoiceSession, VoiceHandle>([[first.control, first.handle]]);
    let cur = first.handle;
    manager = new SessionManager(cfg.rotation ?? { enabled: false, max_turns: 25, max_minutes: 30, max_context_tokens: 24000 }, {
      // the next voice session continues this repair session (same id), muted until the swap
      open: async () => {
        const s = await mintClientSecret({ ...body, session_id: first.handle.sessionId });
        const c = await connectSession(opts, mic, s, engine, spans, stage, setUiHook, { standby: true, onTurn });
        handles.set(c.control, c.handle);
        return c.control;
      },
      summary: () => engine.requestSummary(),
      report: (name, attrs) => {
        spans.record(name, attrs);
        if (name === 'rotated') engine.rotated(String(attrs.reason), Number(attrs.ms));
        if (name === 'connection_dropped') opts.onEvent({ kind: 'status', text: 'connection dropped: reconnecting…' });
        if (name === 'reconnect_gave_up') opts.onEvent({ kind: 'error', text: 'could not reconnect the voice session' });
      },
    });
    manager.adopt(first.control);
    manager.onSwap = (c) => {
      cur = handles.get(c) ?? cur;
      opts.onEvent({ kind: 'status', text: `connected: ${cur.model} (${cur.sessionId})` });
    };
    const timer = window.setInterval(() => void manager?.tick(), 1000);
    // One handle for the page, always pointing at the active voice session.
    return {
      get sessionId() {
        return cur.sessionId;
      },
      get model() {
        return cur.model;
      },
      get config() {
        return cur.config;
      },
      spans,
      get state() {
        return cur.state;
      },
      turnMode: () => cur.turnMode(),
      setTurnMode: (m) => cur.setTurnMode(m),
      pttKey: () => cur.pttKey(),
      setPttKey: (code) => {
        for (const h of handles.values()) h.setPttKey(code);
      },
      pttDown: () => cur.pttDown(),
      pttUp: () => cur.pttUp(),
      clipPlaying: () => cur.clipPlaying(),
      scenario: () => cur.scenario(),
      setScenario: (n) => cur.setScenario(n),
      setLanguage: (l) => cur.setLanguage(l),
      peer: () => cur.peer(),
      stop: () => {
        window.clearInterval(timer);
        manager?.close();
        first.shutdown();
      },
    };
  } catch (err) {
    micP.then((m) => !opts.micStream && m.getTracks().forEach((t) => t.stop())).catch(() => {});
    engine.close();
    throw err;
  }
}

async function connectSession(
  opts: VoiceOptions,
  mic: MediaStream,
  secret: ClientSecretResponse,
  engine: EngineSocket,
  spans: Spans,
  stage: (name: string, attrs?: Record<string, unknown>, t?: number) => void,
  setUiHook: (hook: (ev: UiEvent) => void) => void,
  role: { standby: boolean; onTurn?: () => void },
): Promise<{ handle: VoiceHandle; control: VoiceSession; shutdown: () => void }> {
  // A standby session (rotation, reconnect) connects muted: it doesn't hear the mic, play audio,
  // react to the talk key or take engine notes until activated.
  let active = !role.standby;
  let dropCb = null as ((reason: string) => void) | null;
  let closing = false;
  const { onEvent } = opts;
  const cfg = secret.config;
  const runId = opts.runId ?? null;
  const state: LiveState = {
    responsesInFlight: 0,
    audioPlaying: false,
    toolsPending: 0,
    lastActivity: nowMs(),
    lastSpeechEnd: 0,
    userLang: null,
    sessionLang: null,
    pttDown: false,
    contextTokens: 0,
  };
  const touch = () => {
    state.lastActivity = nowMs();
  };
  let turnMode = cfg.turn_mode;
  let pttKey = opts.pttKey ?? 'Space';
  let preamble: LocalPreamble | null = null;
  let peer: RTCPeerConnection | null = null;

  // Rule 2 backstop: the safety gate's live approval (from its tool results), with the approval
  // lifetime; the engine's power.ended events end it too.
  const approvals = new PowerApprovals();

  const uiHook = (ev: UiEvent) => {
    approvals.onEngineEvent(ev);
    if (ev.event === 'session.lang' && (ev.lang === 'en' || ev.lang === 'hi')) state.sessionLang = ev.lang;
  };
  if (active) setUiHook(uiHook);
  // the engine socket is shared: only the first session opens it (same repair session id)
  const engineReady = role.standby
    ? Promise.resolve()
    : engine
        .connect({ session_id: secret.session_id, run_id: runId, dummy_delay_s: opts.dummyDelayS ?? null, scenario: opts.scenario ?? null })
        .then(() => stage('engine_ready', { scenario: engine.scenario }));
  if (cfg.preamble === 'local') {
    void LocalPreamble.create((n, a, t) => spans.record(n, a, t), opts.preamblePath).then((p) => (preamble = p));
  }

  const tools = cfg.tools.map((spec) =>
    tool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters as never,
      strict: true,
      // Readings and boot watches take seconds; the SDK's own default would cut them off.
      timeoutMs: cfg.tool_timeouts_ms?.[spec.name] ?? 8000,
      timeoutBehavior: 'error_as_result',
      execute: async (input, _ctx, details) => {
        const callId = details?.toolCall?.callId ?? crypto.randomUUID();
        const turn = spans.turn;
        state.toolsPending += 1;
        touch();
        spans.record('tool_call_emitted', { name: spec.name, call_id: callId });
        try {
          const output = await engine.callTool(callId, spec.name, input, turn);
          spans.record('tool_result_received', { name: spec.name, call_id: callId });
          approvals.noteToolOutput(spec.name, output);
          if (preamble) {
            // The value reply must not talk over the clip: hold the output until it has finished.
            const t0 = nowMs();
            await preamble.whenDone(callId);
            if (nowMs() - t0 > 1) spans.record('tool_output_held', { call_id: callId, waited_ms: Math.round(nowMs() - t0) });
          }
          return output;
        } finally {
          state.toolsPending -= 1;
          touch();
        }
      },
    }),
  );

  const agent = new RealtimeAgent({ name: 'Benchmate', instructions: cfg.instructions, tools });

  let stopOnset: (() => void) | null = null;
  const audioElement = document.createElement('audio');
  audioElement.autoplay = true;
  const transport = new OpenAIRealtimeWebRTC({
    mediaStream: mic,
    audioElement,
    changePeerConnection: (pc) => {
      peer = pc;
      stage('pc_offer');
      let lost: number | null = null;
      pc.addEventListener('connectionstatechange', () => {
        if (pc.connectionState === 'connected') {
          stage('pc_connected');
          if (lost !== null) window.clearTimeout(lost);
          lost = null;
          return;
        }
        spans.record('pc_state', { state: pc.connectionState });
        if (closing) return;
        // a dropped connection: reconnect (rotation.ts). "disconnected" can recover; give it 2.5 s.
        if (pc.connectionState === 'failed' || pc.connectionState === 'closed') dropCb?.(`pc_${pc.connectionState}`);
        else if (pc.connectionState === 'disconnected' && lost === null) {
          lost = window.setTimeout(() => pc.connectionState !== 'connected' && !closing && dropCb?.('pc_disconnected'), 2500);
        }
      });
      pc.addEventListener('track', (ev) => {
        if (ev.track.kind !== 'audio' || stopOnset) return;
        stopOnset = watchAudioOnset(ev.streams[0] ?? new MediaStream([ev.track]), () => spans.record('audio_onset'));
      });
      return pc;
    },
  });

  const session = new RealtimeSession(agent, {
    transport,
    model: cfg.model,
    config: sdkConfig(cfg),
    // Checked on the transcript every 12 characters and on the final one; the transcript usually
    // runs ahead of the audio, so a trip often cuts the instruction before it is heard.
    outputGuardrails: [
      {
        name: 'power_instruction_without_gate',
        policyHint: POWER_POLICY_HINT,
        execute: async ({ agentOutput }) => {
          const v = approvals.check(String(agentOutput ?? ''));
          return { tripwireTriggered: v !== null, outputInfo: v ?? {} };
        },
      },
    ],
    outputGuardrailSettings: { debounceTextLength: 12 },
    workflowName: 'benchmate',
    groupId: secret.session_id,
    traceMetadata: { prompt_version: cfg.prompt_version, run_id: runId ?? '' },
  });

  // Barge-in: the user starts talking while the agent speaks or a reply is in flight. The engine
  // cancels any pending power approval, so a power step is never acted on half-heard.
  const bargeInIfBusy = (via: string): boolean => {
    if (!state.audioPlaying && state.responsesInFlight === 0) return false;
    spans.record('barge_in', { via });
    engine.bargeIn();
    return true;
  };

  // Engine notes go in as system items; instructions and tools never change (rule 7). Urgent
  // ones interrupt the agent and get a reply at once; others are context for the next turn.
  const injectNote = (note: EngineNote) => {
    spans.record('engine_note', { urgent: note.urgent, chars: note.text.length });
    if (note.urgent && (state.audioPlaying || state.responsesInFlight > 0)) session.interrupt();
    session.transport.sendEvent({
      type: 'conversation.item.create',
      item: { type: 'message', role: 'system', content: [{ type: 'input_text', text: note.text }] },
    });
    if (note.urgent) session.transport.sendEvent({ type: 'response.create' });
  };

  // One clip per user turn: chained tool calls (reading, then next_step) must not stutter.
  let clipTurn = -1;
  let awaitingFirstAudio = false;
  session.on('transport_event', (ev: TransportEvent) => {
    const e = ev as TransportEvent & Record<string, any>;
    if (opts.traceEvents && typeof e.type === 'string' && !e.type.endsWith('.delta')) {
      spans.record('server_event', { type: e.type, response_id: e.response_id ?? e.response?.id, item: e.item?.type });
    }
    switch (e.type) {
      case 'session.created':
        stage('session_created');
        break;
      case 'session.updated': {
        const input = e.session?.audio?.input ?? {};
        spans.record('session_updated', {
          turn_detection: input.turn_detection?.type ?? null,
          eagerness: input.turn_detection?.eagerness,
          silence_ms: input.turn_detection?.silence_duration_ms,
          transcription: input.transcription?.model ?? null,
          max_output_tokens: e.session?.max_output_tokens,
        });
        break;
      }
      case 'input_audio_buffer.speech_started':
        touch();
        approvals.userTurn(turnMode !== 'ptt' && bargeInIfBusy('vad'));
        spans.record('speech_started', { audio_start_ms: e.audio_start_ms, item_id: e.item_id });
        break;
      case 'input_audio_buffer.speech_stopped': {
        touch();
        const s = spans.record('speech_end', { audio_end_ms: e.audio_end_ms, item_id: e.item_id, mode: turnMode });
        state.lastSpeechEnd = s.t_ms;
        if (active) role.onTurn?.();
        awaitingFirstAudio = true;
        break;
      }
      case 'input_audio_buffer.committed':
        spans.record('committed', { item_id: e.item_id });
        break;
      case 'response.created':
        state.responsesInFlight += 1;
        touch();
        spans.record('response_created', { response_id: e.response?.id });
        break;
      case 'response.output_item.added':
        if (e.item?.type === 'function_call') {
          spans.record('tool_call_started', { call_id: e.item.call_id, name: e.item.name });
          // Local preamble: the earliest signal that a tool call is coming. The clip speaks the
          // session language (engine-chosen), falling back to this turn's tag before the first one.
          if (preamble && !state.audioPlaying && clipTurn !== spans.turn) {
            clipTurn = spans.turn;
            preamble.play(e.item.call_id, state.sessionLang ?? clipLangFor(state.userLang));
          }
        }
        break;
      case 'output_audio_buffer.started': {
        state.audioPlaying = true;
        touch();
        if (preamble?.isPlaying) preamble.stop(); // the model spoke anyway: yield to it
        const s = spans.record('first_audio', { response_id: e.response_id });
        if (awaitingFirstAudio) {
          awaitingFirstAudio = false;
          onEvent({ kind: 'latency', turn: s.turn, firstAudioMs: s.t_ms - state.lastSpeechEnd });
        }
        break;
      }
      case 'output_audio_buffer.stopped':
        state.audioPlaying = false;
        touch();
        spans.record('audio_stopped', { response_id: e.response_id });
        break;
      case 'output_audio_buffer.cleared':
        state.audioPlaying = false;
        touch();
        spans.record('audio_cleared', { response_id: e.response_id });
        break;
      case 'response.done': {
        state.responsesInFlight = Math.max(0, state.responsesInFlight - 1);
        touch();
        const r = e.response ?? {};
        const out = (r.output ?? []) as Array<Record<string, any>>;
        spans.record('response_done', {
          response_id: r.id,
          status: r.status,
          reason: r.status_details?.reason,
          outputs: out.map((o) => o.phase ?? o.type).join(','),
          out_tokens: r.usage?.output_tokens,
          out_audio_tokens: r.usage?.output_token_details?.audio_tokens,
          out_text_tokens: r.usage?.output_token_details?.text_tokens,
          in_tokens: (state.contextTokens = r.usage?.input_tokens ?? state.contextTokens),
          in_cached_tokens: r.usage?.input_token_details?.cached_tokens,
          // the split that prices differ by (engine/telemetry/cost.py)
          in_audio_tokens: r.usage?.input_token_details?.audio_tokens,
          in_text_tokens: r.usage?.input_token_details?.text_tokens,
          in_cached_audio_tokens: r.usage?.input_token_details?.cached_tokens_details?.audio_tokens,
          in_cached_text_tokens: r.usage?.input_token_details?.cached_tokens_details?.text_tokens,
        });
        for (const item of out) {
          if (item.type !== 'message' || item.role !== 'assistant') continue;
          const text = ((item.content ?? []) as Array<Record<string, any>>)
            .map((c) => c.transcript ?? c.text ?? '')
            .join(' ')
            .trim();
          if (!text) continue;
          engine.transcript({ role: 'assistant', text, t_ms: nowMs(), turn: spans.turn, item_id: item.id, response_id: r.id, phase: item.phase });
          onEvent({ kind: 'assistant', text, phase: item.phase });
        }
        break;
      }
      case 'conversation.item.input_audio_transcription.completed': {
        const codes = ((e.languages ?? []) as Array<{ code: string }>).map((l) => l.code);
        state.userLang = tagLang(e.transcript ?? '', codes);
        engine.transcript({ role: 'user', text: e.transcript ?? '', t_ms: nowMs(), turn: spans.turn, item_id: e.item_id, languages: codes });
        onEvent({ kind: 'user', text: e.transcript ?? '', lang: state.userLang });
        break;
      }
      case 'error':
        spans.record('error', { message: String(e.error?.message ?? '').slice(0, 200), code: e.error?.code });
        onEvent({ kind: 'error', text: String(e.error?.message ?? 'realtime error') });
        break;
    }
  });
  session.on('error', (err) => onEvent({ kind: 'error', text: String((err.error as Error)?.message ?? err.error) }));
  session.on('guardrail_tripped', (_ctx, _agent, error, details) => {
    const info = (error.result?.output?.outputInfo ?? {}) as { kind?: string; sentence?: string };
    spans.record('safety_violation', { kind: info.kind, sentence: info.sentence?.slice(0, 200), item_id: details.itemId });
    engine.safetyViolation(info.kind ?? 'unknown', info.sentence ?? '', details.itemId);
    onEvent({ kind: 'error', text: `guardrail: ${info.kind}: ${info.sentence ?? ''}` });
  });

  // --- push-to-talk (OpenAI's WebRTC procedure: clear on down; commit + response.create on up) ---
  const pttDown = () => {
    if (turnMode !== 'ptt' || state.pttDown) return;
    state.pttDown = true;
    touch();
    spans.record('ptt_down');
    approvals.userTurn(bargeInIfBusy('ptt'));
    if (state.audioPlaying || state.responsesInFlight > 0) session.interrupt();
    session.transport.sendEvent({ type: 'input_audio_buffer.clear' });
    session.mute(false);
    onEvent({ kind: 'ptt', down: true });
  };
  const pttUp = () => {
    if (!state.pttDown) return;
    state.pttDown = false;
    touch();
    const s = spans.record('speech_end', { mode: 'ptt' });
    state.lastSpeechEnd = s.t_ms;
    if (active) role.onTurn?.();
    awaitingFirstAudio = true;
    session.transport.sendEvent({ type: 'input_audio_buffer.commit' });
    session.transport.sendEvent({ type: 'response.create' });
    // Keep sending a moment longer so the last packets are not cut, then stop sending room noise.
    window.setTimeout(() => turnMode === 'ptt' && !state.pttDown && session.mute(true), 300);
    onEvent({ kind: 'ptt', down: false });
  };
  const onKeyDown = (e: KeyboardEvent) => {
    if (!active) return;
    if (turnMode !== 'ptt' || (e.code !== pttKey && e.key !== pttKey) || isTyping(e.target)) return;
    e.preventDefault();
    if (!e.repeat) pttDown();
  };
  const onKeyUp = (e: KeyboardEvent) => {
    if (!active) return;
    if (turnMode !== 'ptt' || (e.code !== pttKey && e.key !== pttKey)) return;
    e.preventDefault();
    pttUp();
  };
  window.addEventListener('keydown', onKeyDown);
  window.addEventListener('keyup', onKeyUp);

  // Only turn detection may change mid-session; instructions and tools never do (rule 7).
  const setTurnMode = (mode: string) => {
    if (!(mode in cfg.turn_modes) || mode === turnMode) return;
    session.transport.sendEvent({
      type: 'session.update',
      session: { type: 'realtime', audio: { input: { turn_detection: cfg.turn_modes[mode] } } },
    });
    turnMode = mode;
    state.pttDown = false;
    session.mute(mode === 'ptt');
    spans.record('turn_mode', { mode });
    onEvent({ kind: 'turn_mode', mode });
  };

  if (role.standby) audioElement.muted = true;
  else onEvent({ kind: 'status', text: `connecting ${cfg.model}…` });
  await Promise.all([engineReady, session.connect({ apiKey: secret.client_secret })]);
  traceOutgoing(transport, spans);
  if (role.standby) {
    session.mute(true);
    stage('standby_ready', { model: cfg.model });
  } else {
    engine.setNoteHandler(injectNote);
    stage('ready', { model: cfg.model, prompt_version: cfg.prompt_version, turn_mode: turnMode });
    if (turnMode === 'ptt') session.mute(true);
  }
  recordConnectDetails(spans, peer);
  unlockAudioIfBlocked(audioElement, onEvent);
  if (!role.standby) onEvent({ kind: 'status', text: `connected: ${cfg.model} (${secret.session_id})` });

  const closeSession = () => {
    if (closing) return;
    closing = true;
    window.removeEventListener('keydown', onKeyDown);
    window.removeEventListener('keyup', onKeyUp);
    recordConnectDetails(spans, peer, 'rtt_end');
    session.close();
    stopOnset?.();
    preamble?.close();
  };
  const shutdown = () => {
    if (!opts.micStream) for (const t of mic.getTracks()) t.stop();
    window.setTimeout(() => engine.close(), 300);
    onEvent({ kind: 'status', text: 'disconnected' });
  };
  const handle: VoiceHandle = {
    sessionId: secret.session_id,
    model: cfg.model,
    config: cfg,
    spans,
    state,
    turnMode: () => turnMode,
    setTurnMode,
    pttKey: () => pttKey,
    setPttKey: (code: string) => {
      pttKey = code;
    },
    pttDown,
    pttUp,
    clipPlaying: () => preamble?.isPlaying ?? false,
    scenario: () => engine.scenario,
    setScenario: (name: string) => engine.devScenario(name),
    setLanguage: (lang: PreambleLang) => engine.setLanguage(lang),
    peer: () => peer,
    stop: () => {
      closeSession();
      shutdown();
    },
  };
  sessionCounter += 1;
  const id = `${secret.session_id}#${sessionCounter}`;
  const control: VoiceSession = {
    id,
    activate: () => {
      active = true;
      audioElement.muted = false;
      setUiHook(uiHook);
      engine.setNoteHandler(injectNote);
      session.mute(turnMode === 'ptt');
      spans.record('session_active', { id });
    },
    deactivate: () => {
      active = false;
      if (state.audioPlaying || state.responsesInFlight > 0) session.interrupt();
      session.mute(true);
      audioElement.muted = true;
    },
    close: closeSession,
    inject: (text: string, respond: boolean) => {
      session.transport.sendEvent({
        type: 'conversation.item.create',
        item: { type: 'message', role: 'system', content: [{ type: 'input_text', text }] },
      });
      if (respond) session.transport.sendEvent({ type: 'response.create' });
    },
    onDrop: (cb) => {
      dropCb = cb;
    },
    live: () => ({
      audioPlaying: state.audioPlaying,
      responsesInFlight: state.responsesInFlight,
      toolsPending: state.toolsPending,
      pttDown: state.pttDown,
      lastActivity: state.lastActivity,
    }),
    contextTokens: () => state.contextTokens,
  };
  return { handle, control, shutdown };
}

let sessionCounter = 0;

/** Drop the SDK's stalling item retrieves and trace response events (see channelFilter.ts). */
function traceOutgoing(transport: OpenAIRealtimeWebRTC, spans: Spans): void {
  const dc = transport.connectionState.dataChannel;
  if (dc) filterDataChannel(dc as unknown as { send: (data: string) => void }, (name, attrs) => spans.record(name, attrs));
}

/** SDP exchange timing (resource timing of the /v1/realtime/calls fetch) and network round trip. */
function recordConnectDetails(spans: Spans, pc: RTCPeerConnection | null, rttName = 'rtt'): void {
  if (rttName === 'rtt') {
    const sdp = (performance.getEntriesByType('resource') as PerformanceResourceTiming[])
      .filter((r) => r.name.includes('/v1/realtime/calls'))
      .at(-1);
    if (sdp) {
      spans.record('sdp_exchange', { start: performance.timeOrigin + sdp.startTime, ms: Math.round(sdp.duration) }, performance.timeOrigin + sdp.responseEnd);
    }
  }
  if (!pc) return;
  void pc.getStats().then((stats) => {
    stats.forEach((s) => {
      if (s.type === 'candidate-pair' && s.nominated && s.currentRoundTripTime !== undefined) {
        spans.record(rttName, { ms: Math.round(s.currentRoundTripTime * 1000) });
      }
    });
  });
}

/** Chrome may block autoplay until the page gets a gesture; the foot pedal's key press counts. */
function unlockAudioIfBlocked(el: HTMLAudioElement, onEvent: (ev: VoiceEvent) => void): void {
  void el.play().catch(() => {
    onEvent({ kind: 'audio_locked', locked: true });
    const unlock = () => {
      void el.play().then(() => onEvent({ kind: 'audio_locked', locked: false }));
      window.removeEventListener('pointerdown', unlock);
      window.removeEventListener('keydown', unlock);
    };
    window.addEventListener('pointerdown', unlock);
    window.addEventListener('keydown', unlock);
  });
}
