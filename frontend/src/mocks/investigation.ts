import { http, HttpResponse } from 'msw';
import type { AlertRead, ArtifactRead, EventRef, HoneytokenRead, HoneytokenTrigger } from '../shared/api/investigation';
import { eventKey, type HfEvent } from '../shared/api/types';

/**
 * Моки расследования по общему контракту (contracts/openapi.json, PR #35).
 * Ссылки `event_refs` ведут на события фикстур — «Основание» их находит;
 * одна ссылка намеренно недоступна, чтобы показать «событие недоступно».
 */

const AGENT = '00000000-0000-0000-0000-0000000000a1';
const DEPLOYMENT = '00000000-0000-0000-0000-0000000000d1';
const SESSION = '00000000-0000-0000-0000-0000005e5510';
const ref = (event: string): EventRef => ({ agent_id: AGENT, event_id: `00000000-0000-0000-0000-0000000000${event}` });
/** Событие, которого нет (удалено или ещё не опубликовано). */
const MISSING: EventRef = { agent_id: AGENT, event_id: '00000000-0000-0000-0000-0000000000ff' };

const alerts: AlertRead[] = [{
  id: '00000000-0000-4000-8000-00000000a1e1', rule: 'auth.bruteforce', rule_version: 1, severity: 'high', state: 'open',
  title: '7 неудачных входов за 60 с с 192.0.2.23', window: { from: '2026-10-10T09:00:00Z', to: '2026-10-10T09:01:00Z' },
  count: 7, deployment_id: DEPLOYMENT, session_id: SESSION, source_ip: '192.0.2.23',
  created_at: '2026-10-10T09:01:00Z', updated_at: '2026-10-10T09:01:00Z',
  event_refs: [ref('e1'), MISSING], event_refs_truncated: true,
}];

const tokens: HoneytokenRead[] = [{
  id: '00000000-0000-4000-8000-0000000070c1', deployment_id: DEPLOYMENT, kind: 'url',
  label: '00000000-0000-4000-8000-0000000070c1', created_at: '2026-10-09T12:00:00Z',
  last_triggered_at: '2026-10-10T09:02:00Z', trigger_count: 1,
}];

const triggers: Record<string, HoneytokenTrigger[]> = {
  [tokens[0]!.id]: [{ at: '2026-10-10T09:02:00Z', source: { ip: '198.51.100.7', port: 51345 }, session_id: null, event_refs: [ref('e3')] }],
};

const artifact = (n: string, state: ArtifactRead['state'], patch: Partial<ArtifactRead> = {}): ArtifactRead => ({
  id: `00000000-0000-4000-8000-0000000a7${n}`, session_id: SESSION, deployment_id: DEPLOYMENT, state,
  name: 'x.sh', source_url: 'http://203.0.113.9/x.sh', size_bytes: null, sha256: null, mime: null,
  observed_at: '2026-10-10T09:03:00Z', expires_at: null, error: null,
  event_refs: [ref('e2')], event_refs_truncated: false, ...patch,
});

const artifacts: ArtifactRead[] = [
  artifact('001', 'available', {
    size_bytes: 31, sha256: 'b1946ac92492d2347c6235b4d2611184b1946ac92492d2347c6235b4d2611184', mime: 'text/x-shellscript',
    expires_at: '2026-10-17T09:03:00Z',
  }),
  artifact('002', 'pending', { name: 'payload.bin', source_url: null, observed_at: '2026-10-10T09:04:00Z' }),
  artifact('003', 'failed', { name: '<img src=x onerror=alert(1)>.bin', error: 'Источник недоступен', observed_at: '2026-10-10T09:05:00Z' }),
  artifact('004', 'expired', { name: 'old.tar', observed_at: '2026-10-01T09:00:00Z', expires_at: '2026-10-08T09:00:00Z' }),
];

/** Страница по курсору `c<смещение>`; чужой курсор — 422 INVALID_CURSOR, как у центра. */
function page<T>(all: T[], request: Request) {
  const q = new URL(request.url).searchParams;
  const limit = Math.min(Math.max(Number(q.get('limit') ?? 50) || 50, 1), 200);
  const cursor = q.get('cursor');
  if (cursor !== null && !/^c\d+$/.test(cursor)) {
    return HttpResponse.json({ detail: 'Курсор не соответствует запросу', code: 'INVALID_CURSOR' }, { status: 422 });
  }
  const start = cursor ? Number(cursor.slice(1)) : 0;
  return HttpResponse.json({ items: all.slice(start, start + limit), next_cursor: start + limit < all.length ? `c${start + limit}` : null });
}

const err = (status: number, detail: string, code: string | null = null) => HttpResponse.json({ detail, code }, { status });

/**
 * Обработчики расследования. Доступ, CSRF и роль проверяются снаружи
 * (общие помощники handlers.ts передаются параметрами, без циклического импорта).
 */
export function investigationHandlers(opts: {
  authed: () => boolean;
  role: () => 'admin' | 'operator' | 'viewer';
  csrfFailure: (r: Request) => Response | null;
  events: () => readonly HfEvent[];
}) {
  const guard = () => (opts.authed() ? null : err(401, 'Требуется вход'));
  const byDeployment = <T extends { deployment_id: string }>(all: T[], request: Request) => {
    const d = new URL(request.url).searchParams.get('deployment_id');
    return d ? all.filter((x) => x.deployment_id === d) : all;
  };
  return [
    http.get('/api/v1/alerts', ({ request }) => guard() ?? page(byDeployment(alerts, request), request)),
    http.get('/api/v1/honeytokens', ({ request }) => guard() ?? page(byDeployment(tokens, request), request)),
    http.get('/api/v1/honeytokens/:id/triggers', ({ request, params }) => {
      const bad = guard(); if (bad) return bad;
      const list = triggers[String(params.id)];
      return list ? page(list, request) : err(404, 'Токен не найден');
    }),
    http.get('/api/v1/artifacts', ({ request }) => guard() ?? page(byDeployment(artifacts, request), request)),
    http.get('/api/v1/artifacts/:id/content', ({ params }) => {
      const bad = guard(); if (bad) return bad;
      if (opts.role() === 'viewer') return err(403, 'Требуется роль operator или admin');
      const a = artifacts.find((x) => x.id === params.id);
      if (!a) return err(404, 'Артефакт не найден');
      if (a.state === 'pending') return err(409, 'Файл ещё проверяется', 'ARTIFACT_PENDING');
      if (a.state === 'failed') return err(409, 'Файл не удалось получить', 'ARTIFACT_FAILED');
      if (a.state === 'expired') return err(410, 'Файл удалён по сроку хранения', 'ARTIFACT_EXPIRED');
      return new HttpResponse('#!/bin/sh\necho demo artifact\n', {
        headers: {
          'Content-Type': 'application/octet-stream', 'Content-Disposition': `attachment; filename="${a.name}"`,
          'X-Content-Type-Options': 'nosniff',
        },
      });
    }),
    http.post('/api/v1/events/lookup', async ({ request }) => {
      const bad = guard() ?? opts.csrfFailure(request); if (bad) return bad;
      const body = (await request.json().catch(() => null)) as { refs?: EventRef[] } | null;
      const refs = body?.refs;
      const keys = refs?.map((r) => `${r.agent_id}:${r.event_id}`) ?? [];
      if (!refs || refs.length < 1 || refs.length > 100 || new Set(keys).size !== keys.length) {
        return HttpResponse.json({ detail: [{ loc: ['body', 'refs'], type: 'value_error', msg: '1–100 уникальных пар' }] }, { status: 422 });
      }
      // В моке агент один: событие ищется по event_id (agent_id фикстур — свой).
      const byId = new Map(opts.events().map((e) => [e.event_id, e]));
      const items: HfEvent[] = [];
      const unavailable: EventRef[] = [];
      for (const r of refs) {
        const e = byId.get(r.event_id);
        if (e) items.push(e); else unavailable.push(r);
      }
      return HttpResponse.json({ items: items.filter((e, i, a) => a.findIndex((x) => eventKey(x) === eventKey(e)) === i), unavailable });
    }),
    http.get('/api/v1/iocs.csv', ({ request }) => {
      const bad = guard(); if (bad) return bad;
      const q = new URL(request.url).searchParams;
      const from = Date.parse(q.get('from') ?? ''); const to = Date.parse(q.get('to') ?? '');
      if (Number.isNaN(from) || Number.isNaN(to)) return err(422, 'Нужны обе даты: from и to');
      if (!(from < to)) return err(422, 'from должен быть раньше to');
      if (to - from > 31 * 86_400_000) return err(422, 'Период больше 31 дня');
      const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
      const csv = '﻿indicator_type,indicator_value,occurred_at,agent_id,event_id,deployment_id,session_id\r\n'
        + `ipv4,192.0.2.23,2026-10-10T09:00:05Z,${AGENT},${ref('e1').event_id},${DEPLOYMENT},${SESSION}\r\n`;
      return new HttpResponse(csv, {
        headers: { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': `attachment; filename="honeyforge-iocs-${stamp}.csv"` },
      });
    }),
  ];
}
