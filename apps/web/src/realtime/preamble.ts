// Local instant preamble ("B"): a short pre-recorded clip played the moment the model emits a
// tool call, so the user hears something at once and the model can stay silent until the result.
//
// The clip is played through a local WebRTC loopback (AudioContext -> RTCPeerConnection ->
// RTCPeerConnection -> <audio>), the same kind of path as the model's own voice. Chrome's echo
// canceller always takes WebRTC playout as its reference; whether it also covers a plain
// <audio src=...> depends on the Chrome version and platform, so we do not rely on it.
// If the loopback cannot be set up, the clip falls back to a plain <audio> element.

import { watchAudioOnset } from './telemetry';

export type PreambleLang = 'en' | 'hi';
export type PreamblePath = 'webrtc' | 'element';

type Recorder = (name: string, attrs?: Record<string, unknown>, t?: number) => void;

const CLIP_URLS: Record<PreambleLang, string> = { en: '/preambles/en.wav', hi: '/preambles/hi.wav' };

export class LocalPreamble {
  private playing: { stop: () => void } | null = null;
  private done = new Map<string, Promise<void>>();

  private constructor(
    readonly path: PreamblePath,
    private ctx: AudioContext,
    private buffers: Record<PreambleLang, AudioBuffer>,
    private out: AudioNode,
    private record: Recorder,
    private teardown: () => void,
  ) {}

  static async create(record: Recorder, prefer: PreamblePath = 'webrtc'): Promise<LocalPreamble> {
    const ctx = new AudioContext();
    void ctx.resume();
    const load = async (url: string) => ctx.decodeAudioData(await (await fetch(url)).arrayBuffer());
    const [en, hi] = await Promise.all([load(CLIP_URLS.en), load(CLIP_URLS.hi)]);
    const buffers = { en, hi };
    if (prefer === 'webrtc') {
      try {
        const loop = await webrtcLoopback(ctx);
        const stopOnset = watchAudioOnset(loop.output, () => record('local_preamble_onset'));
        record('preamble_ready', { path: 'webrtc' });
        return new LocalPreamble('webrtc', ctx, buffers, loop.input, record, () => {
          stopOnset();
          loop.close();
        });
      } catch (err) {
        console.warn('preamble loopback failed, using <audio> element', err);
      }
    }
    record('preamble_ready', { path: 'element' });
    return new LocalPreamble('element', ctx, buffers, ctx.destination, record, () => void ctx.close());
  }

  get isPlaying(): boolean {
    return this.playing !== null;
  }

  /** Start the clip for this tool call. `whenDone(callId)` resolves when it has finished. */
  play(callId: string, lang: PreambleLang): void {
    if (this.done.has(callId)) return;
    const src = this.ctx.createBufferSource();
    src.buffer = this.buffers[lang];
    src.connect(this.out);
    const finished = new Promise<void>((resolve) => {
      src.onended = () => {
        this.playing = null;
        this.record('local_preamble_end', { call_id: callId });
        resolve();
      };
    });
    this.done.set(callId, finished);
    this.playing = { stop: () => src.stop() };
    src.start();
    this.record('local_preamble_start', { call_id: callId, lang, path: this.path, clip_ms: Math.round(src.buffer!.duration * 1000) });
    if (this.path === 'element') {
      // No analyser on this path: audible ~ start + the context's output latency.
      const latencyMs = ((this.ctx.outputLatency || 0) + (this.ctx.baseLatency || 0)) * 1000;
      this.record('local_preamble_onset', { estimated: true }, performance.timeOrigin + performance.now() + latencyMs);
    }
  }

  whenDone(callId: string): Promise<void> {
    return this.done.get(callId) ?? Promise.resolve();
  }

  /** Yield to the model if it starts speaking anyway. */
  stop(): void {
    this.playing?.stop();
  }

  close(): void {
    this.stop();
    this.teardown();
  }
}

/** AudioContext -> RTCPeerConnection pair -> <audio>, with the remote side's onset timed. */
async function webrtcLoopback(ctx: AudioContext): Promise<{ input: AudioNode; output: MediaStream; close: () => void }> {
  const dest = ctx.createMediaStreamDestination();
  const a = new RTCPeerConnection();
  const b = new RTCPeerConnection();
  a.onicecandidate = (e) => e.candidate && void b.addIceCandidate(e.candidate);
  b.onicecandidate = (e) => e.candidate && void a.addIceCandidate(e.candidate);
  a.addTrack(dest.stream.getAudioTracks()[0], dest.stream);
  const remote = new Promise<MediaStream>((resolve) => {
    b.ontrack = (e) => {
      // Ask for the smallest jitter buffer: these packets never leave the machine.
      if ('jitterBufferTarget' in e.receiver) (e.receiver as RTCRtpReceiver & { jitterBufferTarget: number }).jitterBufferTarget = 0;
      resolve(e.streams[0]);
    };
  });
  await a.setLocalDescription(await a.createOffer());
  await b.setRemoteDescription(a.localDescription!);
  await b.setLocalDescription(await b.createAnswer());
  await a.setRemoteDescription(b.localDescription!);
  const stream = await Promise.race([
    remote,
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error('loopback timeout')), 3000)),
  ]);
  const el = new Audio();
  el.autoplay = true;
  el.srcObject = stream;
  void el.play().catch(() => {});
  return {
    input: dest,
    output: stream,
    close: () => {
      el.srcObject = null;
      a.close();
      b.close();
      void ctx.close();
    },
  };
}

export const clipLangFor = (lang: string | null | undefined): PreambleLang =>
  lang === 'hi' || lang === 'hinglish' ? 'hi' : 'en';

