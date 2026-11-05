// Outgoing filter on the Realtime data channel (WebRTC).
//
// Why: agents-realtime 0.21.0 sends `conversation.item.retrieve` for every user item once its
// transcription completes. The reply carries the whole item, including the user's audio, and on the
// data channel it holds up every server event behind it for 2-6 s (measured 4 Nov,
// eval/REPORT-2026-11-06.md): the model's reply plays, but response.created and the next tool call
// arrive seconds late. We never use the retrieved item (transcripts come from
// ...transcription.completed), so we don't ask for it. The SDK is pinned at 0.21.0; any upgrade must
// pass channelFilter.test.ts first.
//
// Also records a span for the client events that start, stop or feed responses, including the
// SDK's own (its response.create after a tool result goes through a private sequencer).

export type Recorder = (name: string, attrs?: Record<string, unknown>) => void;

export const DROPPED_CLIENT_EVENTS = new Set(['conversation.item.retrieve']);
export const TRACED_CLIENT_EVENTS = new Set([
  'response.create',
  'response.cancel',
  'conversation.item.create',
  'input_audio_buffer.commit',
  'output_audio_buffer.clear',
]);

type Sendable = { send: (data: string) => void };

export function filterDataChannel(dc: Sendable, record: Recorder): void {
  const send = dc.send.bind(dc) as (data: string) => void;
  dc.send = (data: string) => {
    try {
      const m = JSON.parse(data) as { type?: string; event_id?: string; item_id?: string; item?: { type?: string; role?: string } };
      if (m.type && DROPPED_CLIENT_EVENTS.has(m.type)) {
        record('client_event_dropped', { type: m.type, item_id: m.item_id });
        return;
      }
      if (m.type && TRACED_CLIENT_EVENTS.has(m.type)) {
        record('client_event', { type: m.type, event_id: m.event_id, item: m.item ? `${m.item.type}${m.item.role ? `:${m.item.role}` : ''}` : undefined });
      }
    } catch {
      /* not JSON: pass through */
    }
    send(data);
  };
}
