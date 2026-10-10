/** Проверка настоящего StreamClient на синтетических ответах backend сервисов. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { StreamClient } from '../frontend/src/shared/stream/StreamClient.ts';
const wire = JSON.parse(await readFile(new URL('../contracts/fixtures/telemetry-stream-wire.json', import.meta.url), 'utf8'));
const sockets = [];
const delivered = [];
const timers = [];
let releaseFirst;
let page = 0;
const client = new StreamClient({
  cursor: wire.after,
  keyOf: e => `${e.agent_id}/${e.event_id}`,
  connect: () => {
    const socket = { onopen: null, onmessage: null, onclose: null, onerror: null, close() {} };
    sockets.push(socket);
    return socket;
  },
  fetchGap: async (after, until, next) => {
    if (sockets.length > 1) return wire.reconnect_page;
    if (page++ === 0) {
      assert.equal(after, wire.after);
      assert.equal(until, wire.hello.watermark);
      assert.equal(next, null);
      return await new Promise(resolve => { releaseFirst = () => resolve(wire.pages[0]); });
    }
    assert.equal(next, wire.pages[0].next_cursor);
    return wire.pages[1];
  },
  setTimer: fn => { timers.push(fn); return fn; },
  clearTimer: () => {},
});
client.subscribe(records => delivered.push(...records));
client.start();
const message = (socket, value) => socket.onmessage({ data: JSON.stringify(value) });
message(sockets[0], wire.hello);
message(sockets[0], wire.live);
message(sockets[0], wire.live);
assert.equal(delivered.length, 0);
releaseFirst();
for (let i = 0; i < 20 && client.currentStatus !== 'live'; i++) await new Promise(resolve => setImmediate(resolve));
assert.equal(client.currentStatus, 'live');
assert.deepEqual(delivered.map(r => r.stream_seq), [...wire.pages.flatMap(p => p.items), wire.live].map(r => r.stream_seq));
assert.notEqual(delivered[0].event.agent_id, delivered[1].event.agent_id);
assert.equal(delivered[0].event.event_id, delivered[1].event.event_id);
assert.equal(client.currentCursor, wire.live.stream_seq);
sockets[0].onclose({ code: 1006 });
timers.shift()();
message(sockets[1], wire.reconnect_hello);
for (let i = 0; i < 20 && client.currentStatus !== 'live'; i++) await new Promise(resolve => setImmediate(resolve));
assert.equal(delivered.length, 5);
assert.equal(client.currentCursor, wire.reconnect_hello.watermark);
client.stop();
console.log('StreamClient + backend wire: live buffer, bigint, pagination, pair identity, dedup and reconnect passed');
