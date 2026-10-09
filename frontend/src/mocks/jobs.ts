import { http, HttpResponse } from 'msw';
import deploymentsFixture from '../../../contracts/fixtures/deployments.draft.json';
import type { Deployment, InteractionLevel, PreviewDeployment } from '../shared/api/types';
import type { Command, Job, JobState } from '../features/fleet/commands';

/**
 * Мок ловушек, команд и заданий (specs, «Команды ловушки»). Команд и заданий
 * в контракте стадии 1 нет — это превью этапа 3. Создание развёртывания
 * (`POST /deployments`) отвечает по контракту (DeploymentRead, state=planned);
 * команды и их задания обслуживаются только здесь.
 */
let deployments: PreviewDeployment[] = structuredClone(deploymentsFixture) as PreviewDeployment[];
let jobs = new Map<string, Job>();
const byKey = new Map<string, string>();
let nextJob = 1;
let nextDeployment = 1;
/** Тесты могут заставить задание завершиться ошибкой. */
let failWith: string | null = null;

export function resetMockJobs() {
  deployments = structuredClone(deploymentsFixture) as PreviewDeployment[];
  jobs = new Map();
  byKey.clear();
  nextJob = 1;
  nextDeployment = 1;
  failWith = null;
}

/** Текущие ловушки — для сводки обзора. */
export function currentDeployments(): readonly PreviewDeployment[] { return deployments; }

export function failNextJobs(message: string | null) { failWith = message; }

const STEPS: JobState[] = ['requested', 'leased', 'running', 'succeeded'];

/** Что делает успешная команда с ловушкой. */
function apply(d: PreviewDeployment, command: Command): PreviewDeployment {
  switch (command) {
    case 'start': return { ...d, state: 'running', applied_revision: d.desired_revision };
    case 'stop': return { ...d, state: 'stopped' };
    case 'apply_config': return { ...d, applied_revision: d.desired_revision };
    case 'reset': return { ...d, state: 'running' };
    case 'rotate_credentials': return d;
  }
}

function advance(job: Job): Job {
  if (job.state === 'succeeded' || job.state === 'failed' || job.state === 'expired') return job;
  const next = STEPS[STEPS.indexOf(job.state) + 1]!;
  if (next === 'succeeded') {
    if (job.error) {
      deployments = deployments.map((d) => (d.id === job.deployment_id && d.state === 'deploying' ? { ...d, state: 'failed' } : d));
      return { ...job, state: 'failed' };
    }
    deployments = deployments.map((d) => (d.id === job.deployment_id ? apply(d, job.command) : d));
  }
  return { ...job, state: next };
}

export const jobHandlers = (
  csrfFailure: (r: Request) => Response | null,
  /** Уровень профиля — сервер знает его сам, клиент не присылает. */
  levelOf: (profileId: string) => InteractionLevel | undefined,
) => [
  http.get('/api/v1/deployments', () => HttpResponse.json(deployments)),
  http.post('/api/v1/deployments/:id/commands', async ({ params, request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const { command, idempotency_key } = (await request.json()) as { command: Command; idempotency_key: string };
    const d = deployments.find((x) => x.id === params.id);
    if (!d) return HttpResponse.json({ detail: 'Ловушка не найдена' }, { status: 404 });
    // Повтор того же нажатия — то же задание, а не второе.
    const known = byKey.get(idempotency_key);
    if (known) return HttpResponse.json({ job_id: known }, { status: 202 });
    const active = [...jobs.values()].some((j) => j.deployment_id === d.id && !['succeeded', 'failed', 'expired'].includes(j.state));
    if (active) return HttpResponse.json({ detail: 'У ловушки уже выполняется задание' }, { status: 409 });
    const job: Job = { job_id: `job-${nextJob++}`, deployment_id: d.id, command, state: 'requested', error: failWith };
    jobs.set(job.job_id, job);
    byKey.set(idempotency_key, job.job_id);
    return HttpResponse.json({ job_id: job.job_id }, { status: 202 });
  }),
  // Контракт: создаётся развёртывание в состоянии planned (DeploymentRead).
  http.post('/api/v1/deployments', async ({ request }) => {
    const bad = csrfFailure(request); if (bad) return bad;
    const body = (await request.json()) as { host_id: string; profile_id: string; revision: number };
    const level = levelOf(body.profile_id);
    if (!level) return HttpResponse.json({ detail: 'Профиль не найден' }, { status: 404 });
    const id = `decoy-${level}-${String(nextDeployment++).padStart(2, '0')}-${deployments.length + 1}`;
    const created: PreviewDeployment = {
      id, host_id: body.host_id, interaction_level: level, state: 'planned',
      profile_id: body.profile_id, desired_revision: body.revision, applied_revision: null,
    };
    deployments = [...deployments, created];
    const read: Deployment = {
      id, host_id: body.host_id, profile_id: body.profile_id, revision: body.revision,
      state: 'planned', created_at: new Date().toISOString(),
    };
    return HttpResponse.json(read, { status: 201 });
  }),
  http.get('/api/v1/jobs/:id', ({ params }) => {
    const job = jobs.get(String(params.id));
    if (!job) return HttpResponse.json({ detail: 'Задание не найдено' }, { status: 404 });
    const next = advance(job);
    jobs.set(job.job_id, next);
    return HttpResponse.json(next);
  }),
];
