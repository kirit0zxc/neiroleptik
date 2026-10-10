import { http, HttpResponse } from 'msw';
import events from '../../../contracts/fixtures/events.draft.json';
import hosts from '../../../contracts/fixtures/hosts.draft.json';
import me from '../../../contracts/fixtures/me.draft.json';
import profilesFixture from '../../../contracts/fixtures/profiles.draft.json';
import auditFixture from '../../../contracts/fixtures/audit.draft.json';
import type { HfEvent, InteractionLevel, Profile, ProfileConfigInput, ProfileConfigPublic, Revision } from '../shared/api/types';
import { currentDeployments, jobHandlers, resetMockJobs } from './jobs';
import { publishedEvents, resetMockStream, streamHandlers } from './stream';

/**
 * Моки API по контракту `openapi.json` и фикстурам из `contracts/`.
 * Сессия хранится в памяти обработчиков: вход открывает её, выход закрывает.
 */
// Сессия мока переживает перезагрузку страницы — как cookie настоящего API.
const SESSION_KEY = 'hf-mock-session';
// Значение — роль вошедшего: operator (по умолчанию) или admin для демо аудита.
const session = {
  get: () => { try { return sessionStorage.getItem(SESSION_KEY) !== null; } catch { return false; } },
  role: (): 'admin' | 'operator' => { try { return sessionStorage.getItem(SESSION_KEY) === 'admin' ? 'admin' : 'operator'; } catch { return 'operator'; } },
  set: (on: boolean, role: 'admin' | 'operator' = 'operator') => { try { if (on) sessionStorage.setItem(SESSION_KEY, role === 'admin' ? 'admin' : '1'); else sessionStorage.removeItem(SESSION_KEY); } catch { /* нет хранилища */ } },
};
/** Текущий пользователь мока (AuthRead) по роли сессии. */
const currentMe = () => (session.role() === 'admin'
  ? { ...me, user: { id: 'u-admin-1', username: 'admin', role: 'admin' } }
  : me);
resetMockStream(events as unknown as HfEvent[]);

let profiles: Profile[] = structuredClone(profilesFixture) as Profile[];
/** История ревизий по каждому профилю (RevisionRead). */
let revisionsById = new Map<string, Revision[]>();
let nextId = 1;
/** Непрозрачный курсор привязан к неизменяемому снимку и его фильтрам. */
let snapshotId = 0;
const snapshots = new Map<string, { items: HfEvent[]; filter: string }>();

/** Текущую (на запись) конфигурацию превратить в публичную (без паролей). */
function toPublic(config: ProfileConfigInput): ProfileConfigPublic {
  const { credentials, ...rest } = config;
  return { ...rest, credential_usernames: (credentials ?? []).map((c) => c.username) };
}

function seedRevisions() {
  // Для каждого профиля — история r1..current (мок использует одну конфигурацию).
  revisionsById = new Map(profiles.map((p) => [
    p.id,
    Array.from({ length: p.revision }, (_, i) => ({ revision: i + 1, config: p.config, created_at: p.created_at })),
  ]));
}
seedRevisions();

/** Изменяющий запрос без верного X-CSRF-Token — 403, как у настоящего API. */
function csrfFailure(request: Request) {
  return request.headers.get('X-CSRF-Token') === me.csrf_token
    ? null
    : HttpResponse.json({ detail: 'CSRF-токен отсутствует или неверен' }, { status: 403 });
}

export const handlers = [
  http.post('/api/v1/auth/login', async ({ request }) => {
    const body = (await request.json()) as { username?: string; password?: string };
    if (body.username === 'operator' && body.password === 'operator') {
      session.set(true);
      return HttpResponse.json(currentMe());
    }
    // Демо-администратор: только он видит журнал аудита (как в центре).
    if (body.username === 'admin' && body.password === 'admin') {
      session.set(true, 'admin');
      return HttpResponse.json(currentMe());
    }
    return HttpResponse.json({ detail: 'Неверный логин или пароль' }, { status: 401 });
  }),
  http.post('/api/v1/auth/logout', ({ request }) => { const bad = csrfFailure(request); if (bad) return bad; session.set(false); return new HttpResponse(null, { status: 204 }); }),
  http.get('/api/v1/auth/me', () => (session.get()
    ? HttpResponse.json(currentMe())
    : HttpResponse.json({ detail: 'Требуется вход' }, { status: 401 }))),
  http.get('/api/v1/hosts', () => HttpResponse.json(hosts)),
  ...jobHandlers(csrfFailure, (id) => profiles.find((p) => p.id === id)?.config.interaction_level as InteractionLevel | undefined),
  // Снимок не меняется при публикации live; cursor относится только к /events.
  http.get('/api/v1/events', ({ request }) => {
    const q = new URL(request.url).searchParams;
    const from = q.get('from'); const to = q.get('to');
    const filter = JSON.stringify(['deployment_id', 'type', 'source_ip', 'from', 'to'].map((key) => q.get(key)));
    const all = [...publishedEvents()].reverse().filter((e) =>
      (!q.get('deployment_id') || e.deployment_id === q.get('deployment_id'))
      && (!q.get('type') || e.type === q.get('type'))
      && (!q.get('source_ip') || e.source.ip === q.get('source_ip'))
      // from включительно, to исключительно (согласовано с командой).
      && (!from || Date.parse(e.occurred_at) >= Date.parse(from)) && (!to || Date.parse(e.occurred_at) < Date.parse(to)));
    const cursor = q.get('cursor');
    let id: string;
    let start = 0;
    if (cursor) {
      const parts = /^(s\d+):(\d+)$/.exec(cursor);
      if (!parts || snapshots.get(parts[1]!)?.filter !== filter) {
        return HttpResponse.json({ detail: 'Курсор не соответствует снимку или фильтрам' }, { status: 422 });
      }
      id = parts[1]!;
      start = Number(parts[2]);
    } else {
      id = `s${++snapshotId}`;
      snapshots.set(id, { items: structuredClone(all), filter });
      // Мок не накапливает бесконечную историю снимков при автотестах и демо.
      if (snapshots.size > 100) snapshots.delete(snapshots.keys().next().value!);
    }
    const snapshot = snapshots.get(id)!;
    const limit = Number(q.get('limit') ?? 50);
    const items = snapshot.items.slice(start, start + limit);
    const next = start + limit < snapshot.items.length ? `${id}:${start + limit}` : null;
    return HttpResponse.json({ items, next_cursor: next });
  }),
  ...streamHandlers,

  // Журнал аудита (AuditRead): только admin, новые сверху, limit/offset.
  http.get('/api/v1/audit', ({ request }) => {
    if (!session.get()) return HttpResponse.json({ detail: 'Требуется вход' }, { status: 401 });
    if (session.role() !== 'admin') return HttpResponse.json({ detail: 'Недостаточно прав' }, { status: 403 });
    const q = new URL(request.url).searchParams;
    const limit = Number(q.get('limit') ?? 50);
    const offset = Number(q.get('offset') ?? 0);
    return HttpResponse.json(auditFixture.slice(offset, offset + limit));
  }),

  // Сводка обзора — по текущему состоянию моков (превью этапа 4).
  http.get('/api/v1/analytics/summary', () => {
    const deps = currentDeployments();
    const hourAgo = Date.now() - 3600_000;
    const recent = publishedEvents().filter((e) => Date.parse(e.occurred_at) >= hourAgo);
    const counts = new Map<string, number>();
    for (const e of recent) counts.set(e.source.ip, (counts.get(e.source.ip) ?? 0) + 1);
    return HttpResponse.json({
      deployments: { total: deps.length, running: deps.filter((d) => d.state === 'running').length, failed: deps.filter((d) => d.state === 'failed').length },
      hosts: { total: hosts.length, online: hosts.filter((h) => h.status === 'online').length, offline: hosts.filter((h) => h.status !== 'online').length },
      events_last_hour: recent.length,
      alerts_open: 1,
      top_sources: [...counts].sort((a, b) => b[1] - a[1]).slice(0, 5).map(([ip, count]) => ({ ip, count })),
    });
  }),

  // Профили (ProfileRead/ProfileCreate/ProfileUpdate). Каждое сохранение — новая
  // ревизия. Полную проверку полей делает настоящий API; мок принимает прошедшее схему формы.
  http.get('/api/v1/profiles', () => HttpResponse.json(profiles)),
  http.get('/api/v1/profiles/:id', ({ params }) => {
    const p = profiles.find((x) => x.id === params.id);
    return p ? HttpResponse.json(p) : HttpResponse.json({ detail: 'Профиль не найден' }, { status: 404 });
  }),
  http.get('/api/v1/profiles/:id/revisions', ({ params }) => {
    const revs = revisionsById.get(String(params.id));
    return revs ? HttpResponse.json(revs) : HttpResponse.json({ detail: 'Профиль не найден' }, { status: 404 });
  }),
  http.post('/api/v1/profiles', async ({ request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const { name, description, config } = (await request.json()) as { name: string; description?: string; config: ProfileConfigInput };
    const now = new Date().toISOString();
    const created: Profile = {
      id: `p-new-${nextId++}`, name, description: description ?? '', archived: false,
      revision: 1, config: toPublic(config), created_at: now,
    };
    profiles = [...profiles, created];
    revisionsById.set(created.id, [{ revision: 1, config: created.config, created_at: now }]);
    return HttpResponse.json(created, { status: 201 });
  }),
  http.patch('/api/v1/profiles/:id', async ({ params, request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const p = profiles.find((x) => x.id === params.id);
    if (!p) return HttpResponse.json({ detail: 'Профиль не найден' }, { status: 404 });
    const body = (await request.json()) as { expected_revision: number; name?: string; description?: string; config?: ProfileConfigInput };
    // Конкурентное изменение: ожидаемая ревизия не совпала с текущей (409).
    if (body.expected_revision !== p.revision) {
      return HttpResponse.json({ detail: 'Профиль изменён — перечитайте его' }, { status: 409 });
    }
    const now = new Date().toISOString();
    const revision = p.revision + 1;
    const updated: Profile = {
      ...p, revision, name: body.name ?? p.name, description: body.description ?? p.description,
      config: body.config ? {
        ...toPublic(body.config),
        credential_usernames: body.config.interaction_level === 'low' ? []
          : body.config.credentials == null ? p.config.credential_usernames
          : body.config.credentials.map((credential) => credential.username),
      } : p.config, created_at: now,
    };
    profiles = profiles.map((x) => (x.id === p.id ? updated : x));
    revisionsById.set(p.id, [...(revisionsById.get(p.id) ?? []), { revision, config: updated.config, created_at: now }]);
    return HttpResponse.json(updated);
  }),
];

/** Для тестов: начать с открытой или закрытой сессией и исходными данными. */
export function resetMockSession(open = false, role: 'admin' | 'operator' = 'operator') {
  session.set(open, role);
  resetMockStream(events as unknown as HfEvent[]);
  resetMockJobs();
  profiles = structuredClone(profilesFixture) as Profile[];
  seedRevisions();
  nextId = 1;
  snapshots.clear();
  snapshotId = 0;
}
