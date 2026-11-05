// Stall test (node --test; run by engine/tests/test_web_stall.py and `npm test`).
//
// The real SDK message handler gets the events of a push-to-talk turn with a tool call; whatever
// the SDK sends goes through the page's data-channel filter to a fake server. The fake server
// answers only the client events a turn needs; any other request after a transcript is what
// stalled the channel on 4 Nov (a large reply that holds up every event behind it), so the fake
// server marks itself blocked. If an SDK upgrade starts sending a new request, this fails.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { OpenAIRealtimeWebRTC } from '@openai/agents-realtime';

import { DROPPED_CLIENT_EVENTS, filterDataChannel } from './channelFilter.ts';

const ANSWERED = new Set([
  'session.update',
  'input_audio_buffer.clear',
  'input_audio_buffer.commit',
  'response.create',
  'response.cancel',
  'conversation.item.create',
  'output_audio_buffer.clear',
]);

class FakeServer {
  received: string[] = [];
  blockedBy: string | null = null;
  send(data: string): void {
    let type: string;
    try {
      type = (JSON.parse(data) as { type: string }).type;
    } catch {
      type = '(not json)';
    }
    this.received.push(type);
    if (!ANSWERED.has(type) && this.blockedBy === null) this.blockedBy = type;
  }
}

type Transport = OpenAIRealtimeWebRTC & { _onMessage: (e: { data: string }) => void; sendEvent: (e: unknown) => void };

/** The real SDK transport, with its sends routed to `channel` instead of a live WebRTC call. */
function sdk(channel: { send: (data: string) => void }): Transport {
  const g = globalThis as { RTCPeerConnection?: unknown };
  g.RTCPeerConnection ??= class {}; // the constructor only checks that WebRTC exists
  const t = new OpenAIRealtimeWebRTC() as Transport;
  t.sendEvent = (e: unknown) => channel.send(JSON.stringify(e));
  return t;
}

let n = 0;
const ev = (type: string, extra: Record<string, unknown> = {}) => ({ data: JSON.stringify({ type, event_id: `ev_${++n}`, ...extra }) });

/** One push-to-talk turn: commit, the user's transcript, a response with a tool call. */
function turn(t: Transport): void {
  t._onMessage(ev('input_audio_buffer.committed', { item_id: 'item_user_1', previous_item_id: null }));
  t._onMessage(ev('conversation.item.input_audio_transcription.completed', {
    item_id: 'item_user_1', content_index: 0, transcript: 'haan, sahi hai', usage: { type: 'duration', seconds: 1 },
  }));
  t._onMessage(ev('response.created', { response: { id: 'resp_1', object: 'realtime.response', status: 'in_progress', output: [] } }));
  t._onMessage(ev('response.done', { response: { id: 'resp_1', object: 'realtime.response', status: 'completed', output: [] } }));
}

test('the SDK still requests the transcribed user item (the 4 Nov stall trigger)', () => {
  // Characterisation: if this starts failing after an upgrade, the SDK stopped doing it and the
  // filter may no longer be needed; re-measure before removing it.
  const server = new FakeServer();
  turn(sdk(server));
  assert.ok(server.received.includes('conversation.item.retrieve'), `SDK sent: ${server.received.join(', ')}`);
  assert.equal(server.blockedBy, 'conversation.item.retrieve');
});

test('with the page filter, nothing a turn does not need reaches the server', () => {
  const server = new FakeServer();
  const spans: Array<[string, Record<string, unknown> | undefined]> = [];
  filterDataChannel(server, (name, attrs) => spans.push([name, attrs]));
  turn(sdk(server));
  assert.equal(server.blockedBy, null, `would stall on: ${server.blockedBy}`);
  assert.ok(!server.received.includes('conversation.item.retrieve'));
  assert.deepEqual(spans.filter(([name]) => name === 'client_event_dropped').map(([, a]) => a?.type), ['conversation.item.retrieve']);
});

test('the filter passes everything else through unchanged', () => {
  const server = new FakeServer();
  const spans: string[] = [];
  filterDataChannel(server, (name) => spans.push(name));
  for (const type of ANSWERED) server.send(JSON.stringify({ type }));
  server.send('not json');
  assert.deepEqual(server.received, [...ANSWERED, '(not json)']);
  assert.equal(spans.filter((s) => s === 'client_event').length, 5);
  assert.ok([...DROPPED_CLIENT_EVENTS].every((t) => !ANSWERED.has(t)));
});
