import { useCallback, useEffect, useRef, useState } from 'react';

import { Harness } from './harness';
import { startVoice, type VoiceEvent, type VoiceHandle } from './realtime/voice';

const params = new URLSearchParams(location.search);
const flag = (name: string, fallback: boolean) => (params.has(name) ? params.get(name) !== '0' : fallback);
const delayParam = params.get('delay');
const harnessMode = flag('harness', false);

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

export function App() {
  const [model, setModel] = useState(params.get('model') ?? 'mini');
  const [status, setStatus] = useState('idle');
  const [lines, setLines] = useState<Line[]>([]);
  const [latencies, setLatencies] = useState<{ turn: number; ms: number }[]>([]);
  const [stages, setStages] = useState<Record<string, number>>({});
  const [turnMode, setTurnModeState] = useState(params.get('turn_mode') ?? '');
  const [pttKey, setPttKeyState] = useState(params.get('ptt_key') ?? storedKey ?? 'Space');
  const [pttDown, setPttDown] = useState(false);
  const [audioLocked, setAudioLocked] = useState(false);
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
        case 'ui':
          push({ who: 'engine', text: `${ev.event.event} ${String(ev.event.name ?? '')}` });
          break;
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
        micStream: harness?.stream,
        preamblePath: params.get('preamble_path') === 'element' ? 'element' : 'webrtc',
        onEvent,
      });
      setTurnModeState(handle.current.turnMode());
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
  return (
    <main>
      <header>
        <h1>Benchmate</h1>
        <span className="status" data-testid="status">
          {status}
          {readyMs !== undefined && ` · ready ${Math.round(readyMs)} ms after page load`}
        </span>
      </header>
      {audioLocked && <p className="banner">Click or press any key to turn on the voice.</p>}
      <section className="controls">
        <label>
          Model{' '}
          <select value={model} onChange={(e) => setModel(e.target.value)} disabled={connected}>
            <option value="mini">mini (dev)</option>
            <option value="flagship">flagship (demo)</option>
          </select>
        </label>
        {connected ? (
          <button onClick={disconnect}>Disconnect</button>
        ) : (
          <button onClick={() => void connect()}>Connect</button>
        )}
      </section>
      <section className="controls dev">
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
        {turnMode === 'ptt' && <span className={pttDown ? 'ptt on' : 'ptt'}>{pttDown ? 'listening' : `hold ${pttKey} to talk`}</span>}
      </section>
      <section className="latency">
        <h2>speech end → first audio</h2>
        <ol>
          {latencies.map((l) => (
            <li key={l.turn} className={l.ms > 1200 ? 'slow' : l.ms > 800 ? 'warn' : ''}>
              turn {l.turn}: {Math.round(l.ms)} ms
            </li>
          ))}
        </ol>
        {Object.keys(stages).length > 0 && (
          <p className="stages">
            {Object.entries(stages)
              .filter(([name]) => name !== 'page_load')
              .map(([name, ms]) => `${name} ${Math.round(ms)}`)
              .join(' · ')}
          </p>
        )}
      </section>
      <section className="log">
        {lines.map((l, i) => (
          <p key={i} className={l.who}>
            <b>{l.who}</b> {l.text}
          </p>
        ))}
      </section>
    </main>
  );
}
