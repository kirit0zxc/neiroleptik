import { http, HttpResponse } from 'msw';
import events from '../../../contracts/fixtures/events.draft.json';
import hosts from '../../../contracts/fixtures/hosts.draft.json';
import me from '../../../contracts/fixtures/me.draft.json';
import profilesFixture from '../../../contracts/fixtures/profiles.draft.json';
import type { HfEvent } from '../shared/api/types';
import { currentDeployments, jobHandlers, resetMockJobs } from './jobs';
import { publishedEvents, resetMockStream, streamHandlers } from './stream';

/**
 * Моки API по черновым фикстурам из contracts/ (specs/frontend.md, раздел 4).
 * Сессия хранится в памяти обработчиков: вход открывает её, выход закрывает.
 */
// Сессия мока переживает перезагрузку страницы — как cookie настоящего API.
const SESSION_KEY = 'hf-mock-session';
const session = {
  get: () => { try { return sessionStorage.getItem(SESSION_KEY) === '1'; } catch { return false; } },
  set: (on: boolean) => { try { if (on) sessionStorage.setItem(SESSION_KEY, '1'); else sessionStorage.removeItem(SESSION_KEY); } catch { /* нет хранилища */ } },
};
resetMockStream(events as unknown as HfEvent[]);

type MockProfile = (typeof profilesFixture)[number];
let profiles: MockProfile[] = structuredClone(profilesFixture);
let nextId = 1;

/** Изменяющий запрос без верного X-CSRF-Token — 403, как у настоящего API. */
function csrfFailure(request: Request) {
  return request.headers.get('X-CSRF-Token') === me.csrf_token
    ? null
    : HttpResponse.json({ detail: 'CSRF-токен отсутствует или неверен' }, { status: 403 });
}

export const handlers = [
  http.post('/api/v1/auth/login', async ({ request }) => {
    const body = (await request.json()) as { login?: string; password?: string };
    if (body.login === 'operator' && body.password === 'operator') {
      session.set(true);
      return HttpResponse.json(me);
    }
    return HttpResponse.json({ detail: 'Неверный логин или пароль' }, { status: 401 });
  }),
  http.post('/api/v1/auth/logout', ({ request }) => { const bad = csrfFailure(request); if (bad) return bad; session.set(false); return new HttpResponse(null, { status: 204 }); }),
  http.get('/api/v1/auth/me', () => (session.get()
    ? HttpResponse.json(me)
    : HttpResponse.json({ detail: 'Требуется вход' }, { status: 401 }))),
  http.get('/api/v1/hosts', () => HttpResponse.json(hosts)),
  ...jobHandlers(csrfFailure, (id) => profiles.find((p) => p.profile_id === id)?.config.level as 'low' | 'medium' | 'high' | undefined),
  // Новые сверху, фильтры как у сервера, пагинация курсором (индекс в выборке).
  http.get('/api/v1/events', ({ request }) => {
    const q = new URL(request.url).searchParams;
    const from = q.get('from'); const to = q.get('to');
    const all = [...publishedEvents()].reverse().filter((e) =>
      (!q.get('deployment_id') || e.deployment_id === q.get('deployment_id'))
      && (!q.get('type') || e.type === q.get('type'))
      && (!q.get('source_ip') || e.source.ip === q.get('source_ip'))
      // from включительно, to исключительно (согласовано с командой).
      && (!from || Date.parse(e.occurred_at) >= Date.parse(from)) && (!to || Date.parse(e.occurred_at) < Date.parse(to)));
    const start = Number(q.get('cursor') ?? 0);
    const limit = Number(q.get('limit') ?? 50);
    const items = all.slice(start, start + limit);
    const next = start + limit < all.length ? String(start + limit) : null;
    return HttpResponse.json({ items, next_cursor: next });
  }),
  ...streamHandlers,

  // Сводка обзора — по текущему состоянию моков.
  http.get('/api/v1/analytics/summary', () => {
    const deps = currentDeployments();
    const hourAgo = Date.now() - 3600_000;
    const recent = publishedEvents().filter((e) => Date.parse(e.occurred_at) >= hourAgo);
    const counts = new Map<string, number>();
    for (const e of recent) counts.set(e.source.ip, (counts.get(e.source.ip) ?? 0) + 1);
    return HttpResponse.json({
      deployments: { total: deps.length, running: deps.filter((d) => d.state === 'running').length, failed: deps.filter((d) => d.state === 'failed').length },
      hosts: { total: hosts.length, online: hosts.filter((h) => h.status === 'online').length, degraded: hosts.filter((h) => h.status === 'degraded').length },
      events_last_hour: recent.length,
      alerts_open: 1,
      top_sources: [...counts].sort((a, b) => b[1] - a[1]).slice(0, 5).map(([ip, count]) => ({ ip, count })),
    });
  }),

  // Профили: каждое сохранение — новая ревизия (F9). Проверку полей делает
  // настоящий API; мок принимает то, что прошло схему формы.
  http.get('/api/v1/profiles', () => HttpResponse.json(profiles)),
  http.get('/api/v1/profiles/:id', ({ params }) => {
    const p = profiles.find((x) => x.profile_id === params.id);
    return p ? HttpResponse.json(p) : HttpResponse.json({ detail: 'Профиль не найден' }, { status: 404 });
  }),
  http.post('/api/v1/profiles', async ({ request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const { config } = (await request.json()) as { config: MockProfile['config'] };
    const now = new Date().toISOString();
    const created: MockProfile = {
      profile_id: `p-new-${nextId++}`, revision: 1, updated_at: now,
      revisions: [{ revision: 1, created_at: now, created_by: 'operator' }], config,
    };
    profiles = [...profiles, created];
    return HttpResponse.json(created, { status: 201 });
  }),
  http.patch('/api/v1/profiles/:id', async ({ params, request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const p = profiles.find((x) => x.profile_id === params.id);
    if (!p) return HttpResponse.json({ detail: 'Профиль не найден' }, { status: 404 });
    const { config } = (await request.json()) as { config: MockProfile['config'] };
    const now = new Date().toISOString();
    const revision = p.revision + 1;
    const updated: MockProfile = { ...p, revision, updated_at: now, config,
      revisions: [...p.revisions, { revision, created_at: now, created_by: 'operator' }] };
    profiles = profiles.map((x) => (x.profile_id === p.profile_id ? updated : x));
    return HttpResponse.json(updated);
  }),
];

/** Для тестов: начать с открытой или закрытой сессией и исходными данными. */
export function resetMockSession(open = false) {
  session.set(open);
  resetMockStream(events as unknown as HfEvent[]);
  resetMockJobs();
  profiles = structuredClone(profilesFixture);
  nextId = 1;
}
