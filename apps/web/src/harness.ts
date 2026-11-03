// Latency harness (?harness=1): the "microphone" is an AudioContext stream, and scripted clips
// are played into it one at a time, each after the previous reply has finished. Because the
// page schedules the audio itself, the true start and end of each line are known to within a
// few milliseconds (the fake-WAV approach had about +/-300 ms), and push-to-talk can be pressed
// and released at exact times. Driven by eval/latency_script/run_experiments.py.

import type { VoiceHandle } from './realtime/voice';
import { nowMs } from './realtime/telemetry';

export type LineSpec = {
  id: string;
  url: string;
  set: string;
  kind: 'plain' | 'tool';
  lang: string;
  speech_start_ms: number; // offsets inside the clip
  speech_end_ms: number;
  pause_ms?: number;
  text?: string;
};

export type RunOptions = {
  settleMs?: number; // reply finished = this long with nothing in flight
  lineTimeoutMs?: number;
  pttKey?: string;
  pttLeadMs?: number; // press this long before the clip starts
  pttReleaseLagMs?: number; // release this long after the true end of speech (a human is not instant)
};

export type LineResult = { id: string; true_start: number; true_end: number; timed_out: boolean };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export class Harness {
  readonly ctx = new AudioContext({ sampleRate: 48000 });
  private dest = this.ctx.createMediaStreamDestination();
  private buffers = new Map<string, AudioBuffer>();

  constructor(
    private handle: () => VoiceHandle | null,
    noiseDbfs = -60,
  ) {
    this.dest.channelCount = 1;
    this.dest.channelCountMode = 'explicit';
    void this.ctx.resume();
    // A real microphone always has a noise floor. Without one, an idle WebAudio track sends no
    // packets at all, and the server's VAD never hears the silence that ends a turn.
    const noise = this.ctx.createBuffer(1, this.ctx.sampleRate * 2, this.ctx.sampleRate);
    const amp = Math.pow(10, noiseDbfs / 20) * Math.sqrt(3); // uniform noise with this RMS
    const data = noise.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = (Math.random() * 2 - 1) * amp;
    const src = this.ctx.createBufferSource();
    src.buffer = noise;
    src.loop = true;
    src.connect(this.dest);
    src.start();
  }

  get stream(): MediaStream {
    return this.dest.stream;
  }

  async loadClips(urls: string[]): Promise<number> {
    await Promise.all(
      [...new Set(urls)].filter((u) => !this.buffers.has(u)).map(async (u) => {
        this.buffers.set(u, await this.ctx.decodeAudioData(await (await fetch(u)).arrayBuffer()));
      }),
    );
    return this.buffers.size;
  }

  /** Schedule a clip; returns its wall-clock start. */
  private play(url: string): { startWall: number; durationMs: number; ended: Promise<void> } {
    const buffer = this.buffers.get(url);
    if (!buffer) throw new Error(`clip not loaded: ${url}`);
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.dest);
    const ended = new Promise<void>((r) => (src.onended = () => r()));
    const leadS = 0.05;
    const c = this.ctx.currentTime;
    const n = nowMs();
    src.start(c + leadS);
    return { startWall: n + leadS * 1000, durationMs: buffer.duration * 1000, ended };
  }

  private idle(h: VoiceHandle): boolean {
    const s = h.state;
    return s.responsesInFlight === 0 && !s.audioPlaying && s.toolsPending === 0 && !h.clipPlaying();
  }

  private async waitIdle(h: VoiceHandle, settleMs: number, timeoutMs: number): Promise<boolean> {
    const t0 = nowMs();
    while (nowMs() - t0 < timeoutMs) {
      if (this.idle(h) && nowMs() - h.state.lastActivity >= settleMs) return true;
      await sleep(50);
    }
    return false;
  }

  private key(type: 'keydown' | 'keyup', code: string): void {
    window.dispatchEvent(new KeyboardEvent(type, { code, key: code === 'Space' ? ' ' : code, bubbles: true }));
  }

  async runLines(lines: LineSpec[], opts: RunOptions = {}): Promise<LineResult[]> {
    const { settleMs = 1200, lineTimeoutMs = 25000, pttLeadMs = 300, pttReleaseLagMs = 200 } = opts;
    const results: LineResult[] = [];
    for (const line of lines) {
      const h = this.handle();
      if (!h) throw new Error('voice session not connected');
      await this.waitIdle(h, settleMs, lineTimeoutMs);
      const ptt = h.turnMode() === 'ptt';
      const code = opts.pttKey ?? h.pttKey();
      if (ptt) {
        this.key('keydown', code);
        await sleep(pttLeadMs);
      }
      const { startWall, durationMs, ended } = this.play(line.url);
      const trueStart = startWall + line.speech_start_ms;
      const trueEnd = startWall + line.speech_end_ms;
      h.spans.record(
        'line_start',
        { id: line.id, set: line.set, kind: line.kind, lang: line.lang, true_start: trueStart, true_end: trueEnd, clip_ms: Math.round(durationMs), pause_ms: line.pause_ms ?? 0 },
        startWall,
      );
      if (ptt) {
        await sleep(Math.max(0, trueEnd + pttReleaseLagMs - nowMs()));
        this.key('keyup', code);
      }
      await ended;
      // Wait for the end-of-turn signal that belongs to the end of this line, then for the reply to finish.
      const t0 = nowMs();
      while (h.state.lastSpeechEnd < trueEnd - 200 && nowMs() - t0 < 6000) await sleep(25);
      const settled = await this.waitIdle(h, settleMs, lineTimeoutMs);
      h.spans.record('line_done', { id: line.id, timed_out: !settled });
      results.push({ id: line.id, true_start: trueStart, true_end: trueEnd, timed_out: !settled });
    }
    return results;
  }
}
