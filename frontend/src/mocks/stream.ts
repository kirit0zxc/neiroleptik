import { DEMO_SESSION } from './replay';
import { http, HttpResponse, ws } from 'msw';
import type { HfEvent } from '../shared/api/types';
import type { StreamRecord } from '../shared/stream/StreamClient';

/**
 * Мок журнала публикаций по согласованному протоколу (specs, раздел 5):
 *   WS:   {"type":"hello","watermark":"N"}, затем {"type":"event","stream_seq":"N+1","event":{…}}
 *   REST: GET /stream-events?after=&until=&limit= → {items, watermark, next_cursor}
 * Номера — строки.
 */
let journal: StreamRecord<HfEvent>[] = [];
const clients = new Set<{ send(data: string): void }>();

const watermark = () => journal.at(-1)?.stream_seq ?? '0';

export function resetMockStream(seed: HfEvent[] = []) {
  journal = seed.map((event, i) => ({ stream_seq: String(i + 1), event }));
}

/** Опубликовать событие: в журнал и всем открытым сокетам. */
export function publish(event: HfEvent) {
  const record = { stream_seq: String(BigInt(watermark()) + 1n), event };
  journal.push(record);
  const data = JSON.stringify({ type: 'event', ...record });
  for (const c of clients) c.send(data);
}

export function publishedEvents(): HfEvent[] { return journal.map((r) => r.event); }

// Регулярка, а не шаблон: шаблон не совпадает, если в адресе есть параметры.
const socket = ws.link(/^wss?:\/\/[^/]+\/api\/v1\/stream(\?.*)?$/);

export const streamHandlers = [
  http.get('/api/v1/stream-events', ({ request }) => {
    const q = new URL(request.url).searchParams;
    const after = BigInt(q.get('after') ?? '0');
    const until = BigInt(q.get('until') ?? watermark());
    const limit = Number(q.get('limit') ?? 100);
    const range = journal.filter((r) => BigInt(r.stream_seq) > after && BigInt(r.stream_seq) <= until);
    const items = range.slice(0, limit);
    const next = items.length < range.length ? items.at(-1)!.stream_seq : null;
    return HttpResponse.json({ items, watermark: String(until), next_cursor: next });
  }),
  socket.addEventListener('connection', ({ client }) => {
    clients.add(client);
    client.send(JSON.stringify({ type: 'hello', watermark: watermark() }));
    client.addEventListener('close', () => clients.delete(client));
  }),
];

// ─── Демо-атаки для режима моков в браузере ───────────────────

const SCRIPT: Array<Pick<HfEvent, 'type' | 'deployment_id' | 'data'> & { ip: string; protocol: string; port: number }> = [
  { type: 'connection.opened', deployment_id: 'decoy-medium-01', ip: '203.0.113.50', protocol: 'ssh', port: 2222, data: {} },
  { type: 'auth.attempt', deployment_id: 'decoy-medium-01', ip: '203.0.113.50', protocol: 'ssh', port: 2222, data: { username: 'root', password: '123456', success: false } },
  { type: 'auth.attempt', deployment_id: 'decoy-medium-01', ip: '203.0.113.50', protocol: 'ssh', port: 2222, data: { username: 'admin', password: 'admin123', success: true } },
  { type: 'session.command', deployment_id: 'decoy-medium-01', ip: '203.0.113.50', protocol: 'ssh', port: 2222, data: { input: 'uname -a; cat /etc/passwd', truncated: false } },
  { type: 'http.request', deployment_id: 'decoy-low-01', ip: '198.51.100.99', protocol: 'http', port: 8080, data: { method: 'POST', path: '[скрыто]', captured_bytes: 64, mapper_truncated: false, capture_completeness: 'unknown', http_parse_state: 'parsed' } },
  { type: 'tcp.request', deployment_id: 'decoy-low-01', ip: '198.51.100.99', protocol: 'tcp', port: 2222, data: { captured_bytes: 3, mapper_truncated: false, capture_completeness: 'unknown' } },
];

let demoSeq = 0;

/** Очередное событие сценария — с уникальным ID и текущим временем. */
export function nextDemoEvent(now = new Date()): HfEvent {
  const step = SCRIPT[demoSeq % SCRIPT.length]!;
  demoSeq += 1;
  const ssh = step.protocol === 'ssh';
  return {
    schema_version: 1, event_id: `demo-${demoSeq}`, agent_id: 'agent-runtime-01', deployment_id: step.deployment_id,
    instance_id: ssh ? 'cowrie-0003' : null, session_id: ssh ? DEMO_SESSION : null, source_seq: demoSeq,
    occurred_at: now.toISOString(), received_at: new Date(now.getTime() + 200).toISOString(), type: step.type,
    source: { ip: step.ip, port: 50000 + demoSeq }, service: { protocol: step.protocol, port: step.port },
    data: step.data, artifact_ids: [], profile_revision: 1,
  };
}
