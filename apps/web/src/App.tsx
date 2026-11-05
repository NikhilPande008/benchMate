import { useCallback, useEffect, useRef, useState } from 'react';

import { Harness } from './harness';
import { EngineSocket } from './realtime/engine';
import { startVoice, type VoiceEvent, type VoiceHandle } from './realtime/voice';
import { Screen } from './screen/Screen';
import { apply, EMPTY, type ScreenState, type UiEvent } from './screen/state';

const params = new URLSearchParams(location.search);
const flag = (name: string, fallback: boolean) => (params.has(name) ? params.get(name) !== '0' : fallback);
const delayParam = params.get('delay');
const harnessMode = flag('harness', false);
const demoMode = flag('demo', false);
// Screen only: a companion screen on a session (no microphone, no voice), e.g. a second monitor,
// the replay, or the UI tests. ?screen=1&session=<id>
const screenOnly = flag('screen', false);

const TURN_MODES: [string, string][] = [
  ['semantic_medium', 'Semantic VAD, medium'],
  ['semantic_high', 'Semantic VAD, high'],
  ['server_300', 'Server VAD, 300 ms'],
  ['server_400', 'Server VAD, 400 ms'],
  ['server_500', 'Server VAD, 500 ms'],
  ['ptt', 'Push-to-talk'],
];

const storedKey = (() => {
  try {
    return localStorage.getItem('benchmate.pttKey');
  } catch {
    return null;
  }
})();

type Line = { who: 'you' | 'benchmate' | 'engine' | 'error' | 'status'; text: string };

/** One readable line per engine UI event for the log. */
function describe(ev: Record<string, unknown>): string {
  switch (ev.event) {
    case 'tool.started':
    case 'tool.finished':
      return `${ev.event} ${String(ev.name)}`;
    case 'ledger.updated':
      return `ledger ${String(ev.measurement)}=${String(ev.outcome)}${ev.conflict ? ` conflict:${String(ev.conflict)}` : ''}${ev.stop ? ' stop' : ''}`;
    case 'step.proposed':
      return `step ${String(ev.step)} ${String(ev.measurement ?? '')}`;
    case 'power.approved':
    case 'power.blocked':
    case 'power.cancelled':
      return `${ev.event} ${String(ev.step)}${ev.reason ? ` (${String(ev.reason)})` : ''}`;
    case 'watch.sample':
      return `watch ${String(ev.t_s)} s ${String(ev.current_a)} A`;
    default:
      return String(ev.event);
  }
}

declare global {
  interface Window {
    benchmate?: {
      status: string;
      handle: VoiceHandle | null;
      lines: Line[];
      stages: Record<string, number>;
      stop: () => void;
    };
    harness?: Harness;
  }
}

const harness = harnessMode ? new Harness(() => window.benchmate?.handle ?? null) : null;
if (harness) window.harness = harness;

/** The companion screen alone, attached to a session over the engine WebSocket. */
function ScreenOnly() {
  const sessionId = params.get('session') ?? 'se_screen';
  const [state, setState] = useState<ScreenState>(EMPTY);
  const [connected, setConnected] = useState(false);
  const socket = useRef<EngineSocket | null>(null);
  useEffect(() => {
    const sock = new EngineSocket((ev) => setState((s) => apply(s, ev as UiEvent)));
    socket.current = sock;
    sock
      .connect({ session_id: sessionId, role: 'screen', scenario: params.get('scenario') })
      .then(() => setConnected(true))
      .catch(() => setConnected(false));
    return () => {
      sock.close();
      setConnected(false);
    };
  }, [sessionId]);
  return <Screen state={state} sessionId={sessionId} demo={demoMode} connected={connected} onLanguage={(l) => socket.current?.setLanguage(l)} />;
}

export function App() {
  return screenOnly ? <ScreenOnly /> : <VoiceApp />;
}

function VoiceApp() {
  const [model, setModel] = useState(params.get('model') ?? 'mini');
  const [status, setStatus] = useState('idle');
  const [lines, setLines] = useState<Line[]>([]);
  const [latencies, setLatencies] = useState<{ turn: number; ms: number }[]>([]);
  const [stages, setStages] = useState<Record<string, number>>({});
  const [turnMode, setTurnModeState] = useState(params.get('turn_mode') ?? '');
  const [pttKey, setPttKeyState] = useState(params.get('ptt_key') ?? storedKey ?? 'Space');
  const [pttDown, setPttDown] = useState(false);
  const [audioLocked, setAudioLocked] = useState(false);
  const [scenario, setScenarioState] = useState<string | null>(params.get('scenario'));
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [screen, setScreen] = useState<ScreenState>(EMPTY);
  const handle = useRef<VoiceHandle | null>(null);
  const started = useRef(false);

  const push = useCallback((line: Line) => setLines((ls) => [...ls.slice(-200), line]), []);

  const onEvent = useCallback(
    (ev: VoiceEvent) => {
      switch (ev.kind) {
        case 'status':
          setStatus(ev.text);
          break;
        case 'user':
          push({ who: 'you', text: `${ev.text}  [${ev.lang}]` });
          break;
        case 'assistant':
          push({ who: 'benchmate', text: ev.text });
          break;
        case 'ui': {
          const u = ev.event;
          if (u.event === 'watch.sample') break; // too chatty for the log
          setScreen((s) => apply(s, u as UiEvent));
          if (u.event === 'screen.state' || u.event === 'transcript') break; // shown on the screen itself
          if (u.event === 'dev.scenario') setScenarioState(String(u.scenario));
          push({ who: 'engine', text: describe(u) });
          break;
        }
        case 'latency':
          setLatencies((ls) => [...ls.slice(-30), { turn: ev.turn, ms: ev.firstAudioMs }]);
          break;
        case 'stage':
          setStages((s) => ({ ...s, [ev.name]: ev.ms }));
          break;
        case 'turn_mode':
          setTurnModeState(ev.mode);
          break;
        case 'ptt':
          setPttDown(ev.down);
          break;
        case 'audio_locked':
          setAudioLocked(ev.locked);
          break;
        case 'error':
          push({ who: 'error', text: ev.text });
          break;
      }
    },
    [push],
  );

  const connect = useCallback(async () => {
    if (handle.current) return;
    setStatus('connecting…');
    try {
      handle.current = await startVoice({
        model,
        runId: params.get('run'),
        dummyDelayS: delayParam === null ? null : Number(delayParam),
        turnMode: params.get('turn_mode'),
        prompt: params.get('prompt'),
        outputCap: params.has('cap') ? flag('cap', true) : null,
        usePool: flag('pool', true),
        pttKey,
        scenario: params.get('scenario'),
        traceEvents: flag('trace', false),
        micStream: harness?.stream,
        preamblePath: params.get('preamble_path') === 'element' ? 'element' : 'webrtc',
        onEvent,
      });
      setTurnModeState(handle.current.turnMode());
      setScenarios(handle.current.config.dev?.scenarios ?? []);
      setScenarioState(handle.current.scenario());
      const lang = params.get('lang');
      if (lang === 'hi' || lang === 'en') handle.current.setLanguage(lang);
    } catch (err) {
      setStatus('error');
      push({ who: 'error', text: String((err as Error).message ?? err) });
    }
  }, [model, onEvent, push, pttKey]);

  const disconnect = useCallback(() => {
    handle.current?.stop();
    handle.current = null;
  }, []);

  // Connect early: open the mic and the session on page load, before the first word.
  useEffect(() => {
    if (flag('autoconnect', true) && !started.current) {
      started.current = true;
      void connect();
    }
  }, [connect]);

  useEffect(() => {
    window.benchmate = { status, handle: handle.current, lines, stages, stop: disconnect };
  }, [status, lines, stages, disconnect]);

  const changeTurnMode = (mode: string) => {
    handle.current?.setTurnMode(mode);
  };
  const changePttKey = (code: string) => {
    setPttKeyState(code);
    handle.current?.setPttKey(code);
    try {
      localStorage.setItem('benchmate.pttKey', code);
    } catch {
      /* private mode */
    }
  };

  const connected = status.startsWith('connected');
  const readyMs = stages.ready;
  const dev = (
    <div className="devbar" data-testid="dev-controls">
      <span className="status" data-testid="status">
        {status}
        {readyMs !== undefined && ` · ready ${Math.round(readyMs)} ms after page load`}
      </span>
      <label>
        Model{' '}
        <select value={model} onChange={(e) => setModel(e.target.value)} disabled={connected}>
          <option value="mini">mini (dev)</option>
          <option value="flagship">flagship (demo)</option>
        </select>
      </label>
      {connected ? <button onClick={disconnect}>Disconnect</button> : <button onClick={() => void connect()}>Connect</button>}
      <label>
        Turn mode{' '}
        <select value={turnMode} onChange={(e) => changeTurnMode(e.target.value)} disabled={!connected}>
          {TURN_MODES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Talk key{' '}
        <input
          className="key"
          value={pttKey}
          readOnly
          onKeyDown={(e) => {
            e.preventDefault();
            changePttKey(e.code);
          }}
          title="Focus and press the key (or foot pedal) to use for push-to-talk"
        />
      </label>
      {scenarios.length > 0 && (
        <label>
          Fake bench{' '}
          <select value={scenario ?? ''} onChange={(e) => handle.current?.setScenario(e.target.value)} disabled={!connected} data-testid="scenario">
            {scenarios.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      )}
      <details className="log">
        <summary>Engine log ({lines.length})</summary>
        {lines.map((l, i) => (
          <p key={i} className={l.who}>
            <b>{l.who}</b> {l.text}
          </p>
        ))}
      </details>
      {Object.keys(stages).length > 0 && (
        <p className="stages">
          {Object.entries(stages)
            .filter(([name]) => name !== 'page_load')
            .map(([name, ms]) => `${name} ${Math.round(ms)}`)
            .join(' · ')}
        </p>
      )}
    </div>
  );
  const ptt = turnMode === 'ptt' && connected ? <span className={pttDown ? 'ptt on' : 'ptt'}>{pttDown ? 'LISTENING' : `hold ${pttKey} to talk`}</span> : null;
  return (
    <main>
      {audioLocked && <p className="banner warn">Click or press any key to turn on the voice.</p>}
      <Screen
        state={screen}
        sessionId={handle.current?.sessionId ?? ''}
        demo={demoMode}
        connected={connected}
        onLanguage={(l) => handle.current?.setLanguage(l)}
        latencies={latencies}
        devControls={dev}
        pttHint={ptt}
      />
    </main>
  );
}
