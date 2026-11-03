// Per-turn latency spans. A turn starts when the client receives
// input_audio_buffer.speech_stopped ("speech_end"); later spans carry that turn index.

import type { EngineSocket, SpanMsg } from './engine';

export const nowMs = (): number => performance.timeOrigin + performance.now();

export class Spans {
  turn = 0;
  readonly log: SpanMsg[] = [];

  constructor(private engine: EngineSocket) {}

  record(name: string, attrs?: Record<string, unknown>, t_ms: number = nowMs()): SpanMsg {
    if (name === 'speech_end') this.turn += 1;
    const span: SpanMsg = { name, t_ms, turn: this.turn, ...(attrs ? { attrs } : {}) };
    this.log.push(span);
    this.engine.span(span);
    return span;
  }
}

/**
 * Detects when model audio becomes audible on the remote WebRTC track (the closest we can get
 * to "the user hears the first word"). Calls onOnset after >= minSilenceMs of silence.
 */
export function watchAudioOnset(
  stream: MediaStream,
  onOnset: () => void,
  { threshold = 0.01, minSilenceMs = 300, pollMs = 10 } = {},
): () => void {
  const ctx = new AudioContext();
  void ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser); // not connected to destination: the <audio> element does playback
  const buf = new Float32Array(analyser.fftSize);
  let lastSound = -Infinity;
  const timer = window.setInterval(() => {
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (const v of buf) sum += v * v;
    const rms = Math.sqrt(sum / buf.length);
    const t = performance.now();
    if (rms > threshold) {
      if (t - lastSound >= minSilenceMs) onOnset();
      lastSound = t;
    }
  }, pollMs);
  return () => {
    window.clearInterval(timer);
    void ctx.close();
  };
}
