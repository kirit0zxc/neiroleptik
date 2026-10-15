import { http, HttpResponse } from 'msw';
import deploymentsFixture from '../../../contracts/fixtures/deployments.draft.json';
import type { Deployment, InteractionLevel } from '../shared/api/types';
import type { Command, Job, JobState } from '../features/fleet/commands';

/**
 * Мок ловушек, команд и заданий строго по контракту центра
 * (contracts/openapi.json, specs/commands.md): DeploymentRead, CommandCreate
 * с expected_generation, JobRead с error_code, коды отказов 409. Используется
 * в тестах и демо; поведение должно совпадать с настоящим API.
 */
type Outcome = { kind: 'fail'; code: string; message: string } | { kind: 'expire'; stage: 'delivery' | 'execution' } | null;
/** Задание мока: JobRead плюс заданный тестом исход (наружу не отдаётся). */
type MockJob = Job & { outcome: Outcome };

let deployments: Deployment[] = structuredClone(deploymentsFixture) as Deployment[];
let jobs = new Map<string, MockJob>();
/** Тело первой отправки по паре (ловушка, idempotency_key). */
const byKey = new Map<string, { job_id: string; command: Command; expected_generation: number }>();
let nextDeployment = 1;
/** Тесты и демо могут задать исход следующих заданий. */
let outcome: Outcome = null;

export function resetMockJobs() {
  deployments = structuredClone(deploymentsFixture) as Deployment[];
  jobs = new Map();
  byKey.clear();
  nextDeployment = 1;
  outcome = null;
}

/** Текущие ловушки — для сводки обзора. */
export function currentDeployments(): readonly Deployment[] { return deployments; }

/** Следующие задания завершатся ошибкой драйвера с этим кодом и текстом. */
export function failNextJobs(code: string, message: string) { outcome = { kind: 'fail', code, message }; }
/**
 * Следующие задания истекут, как в центре: `delivery` — агент не взял команду
 * (состояние ловушки не меняется), `execution` — взял, но не подтвердил
 * (центр не знает итога: ловушка становится unknown).
 */
export function expireNextJobs(stage: 'delivery' | 'execution' = 'delivery') { outcome = { kind: 'expire', stage }; }

/** Состояния, из которых центр принимает команду (specs/commands.md). */
const ALLOWED: Record<Command, readonly Deployment['state'][]> = {
  start: ['planned', 'stopped', 'failed'],
  stop: ['running', 'failed', 'unknown'],
  reset: ['running', 'failed'],
};

const FINAL: readonly JobState[] = ['succeeded', 'failed', 'expired'];
const reject = (code: string, detail: string) => HttpResponse.json({ detail, code }, { status: 409 });
const update = (id: string, patch: Partial<Deployment>) => {
  deployments = deployments.map((d) => (d.id === id ? { ...d, ...patch } : d));
};

/** Один шаг задания за опрос: requested → leased → running → итог. */
function advance(job: MockJob): MockJob {
  if (FINAL.includes(job.state)) return job;
  const now = new Date().toISOString();
  const d = deployments.find((x) => x.id === job.deployment_id);
  if (!d) return job;
  if (job.state === 'requested' && job.outcome?.kind === 'expire' && job.outcome.stage === 'delivery') {
    return { ...job, state: 'expired', error: 'Агент не взял команду', error_code: 'DELIVERY_EXPIRED', completed_at: now };
  }
  // Агент взял команду: только теперь центр показывает переходное состояние.
  if (job.state === 'requested') {
    if (d.state !== 'unknown') update(d.id, { state: job.command === 'stop' ? 'stopping' : 'deploying' });
    return { ...job, state: 'leased' };
  }
  if (job.state === 'leased') return { ...job, state: 'running' };
  if (job.outcome?.kind === 'expire') {
    update(d.id, { state: 'unknown' });
    return { ...job, state: 'expired', error: 'Истёк срок выполнения', error_code: 'EXECUTION_EXPIRED', completed_at: now };
  }
  // running → итог: применяем результат к ловушке, как ACK агента.
  if (job.outcome?.kind === 'fail') {
    update(d.id, { state: job.command === 'stop' ? 'running' : 'failed' });
    return { ...job, state: 'failed', error: job.outcome.message, error_code: job.outcome.code, completed_at: now };
  }
  if (job.command === 'stop') update(d.id, { state: 'stopped' });
  else update(d.id, { state: 'running', applied_revision: d.revision, instance_id: crypto.randomUUID() });
  return { ...job, state: 'succeeded', completed_at: now };
}

export const jobHandlers = (
  csrfFailure: (r: Request) => Response | null,
  /** Уровень профиля — сервер знает его сам, клиент не присылает. */
  levelOf: (profileId: string) => InteractionLevel | undefined,
) => [
  http.get('/api/v1/deployments', () => HttpResponse.json(deployments)),
  http.post('/api/v1/deployments/:id/commands', async ({ params, request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const body = (await request.json()) as { command: Command; idempotency_key: string; expected_generation: number };
    const d = deployments.find((x) => x.id === params.id);
    if (!d) return HttpResponse.json({ detail: 'Назначение не найдено' }, { status: 404 });
    // Повтор той же пары (ловушка, ключ) с тем же телом — прежнее задание.
    const key = `${d.id} ${body.idempotency_key}`;
    const previous = byKey.get(key);
    if (previous) {
      return previous.command === body.command && previous.expected_generation === body.expected_generation
        ? HttpResponse.json({ job_id: previous.job_id }, { status: 202 })
        : reject('IDEMPOTENCY_CONFLICT', 'Ключ использован с другим запросом');
    }
    if ([...jobs.values()].some((j) => j.deployment_id === d.id && !FINAL.includes(j.state))) {
      return reject('COMMAND_BUSY', 'У ловушки уже есть незавершённая команда');
    }
    if (body.expected_generation !== d.generation) return reject('STALE_GENERATION', 'Перечитайте поколение ловушки');
    if (!ALLOWED[body.command]?.includes(d.state)) return reject('INVALID_STATE', 'Команда недоступна в текущем состоянии');
    if (body.command === 'reset' && d.interaction_level !== 'high') return reject('UNSUPPORTED_COMMAND', 'reset разрешён только для High');

    // Создание команды только увеличивает поколение; состояние меняется, когда агент её возьмёт.
    const generation = d.generation + 1;
    update(d.id, { generation });
    const job: MockJob = {
      job_id: crypto.randomUUID(), deployment_id: d.id, command: body.command, state: 'requested',
      error: null, error_code: null, generation, created_at: new Date().toISOString(), completed_at: null,
      outcome,
    };
    jobs.set(job.job_id, job);
    byKey.set(key, { job_id: job.job_id, command: body.command, expected_generation: body.expected_generation });
    return HttpResponse.json({ job_id: job.job_id }, { status: 202 });
  }),
  // Удаление ловушки (F6a): 204 — удалена и резерв порта освобождён; 404 — уже нет;
  // 409 — только из stopped/failed (иначе исполнитель может держать порт).
  http.delete('/api/v1/deployments/:id', ({ params, request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const d = deployments.find((x) => x.id === params.id);
    if (!d) return HttpResponse.json({ detail: 'Ловушка не найдена' }, { status: 404 });
    if (d.state !== 'stopped' && d.state !== 'failed') {
      return reject('DELETE_NOT_ALLOWED', 'Удалить можно только остановленную ловушку или ловушку в ошибке');
    }
    deployments = deployments.filter((x) => x.id !== d.id);
    return new HttpResponse(null, { status: 204 });
  }),
  // Контракт: создаётся развёртывание в состоянии planned (DeploymentRead).
  http.post('/api/v1/deployments', async ({ request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const body = (await request.json()) as { host_id: string; profile_id: string; revision: number };
    const level = levelOf(body.profile_id);
    if (!level) return HttpResponse.json({ detail: 'Профиль не найден' }, { status: 404 });
    const created: Deployment = {
      id: `decoy-${level}-${String(nextDeployment++).padStart(2, '0')}-${deployments.length + 1}`,
      host_id: body.host_id, profile_id: body.profile_id, revision: body.revision, state: 'planned',
      interaction_level: level, desired_revision: body.revision, applied_revision: null,
      generation: 0, instance_id: null, created_at: new Date().toISOString(),
    };
    deployments = [...deployments, created];
    return HttpResponse.json(created, { status: 201 });
  }),
  http.get('/api/v1/jobs/:id', ({ params }) => {
    const job = jobs.get(String(params.id));
    if (!job) return HttpResponse.json({ detail: 'Задание не найдено' }, { status: 404 });
    const next = advance(job);
    jobs.set(job.job_id, next);
    const { outcome: _hidden, ...read } = next;
    return HttpResponse.json(read satisfies Job);
  }),
];
