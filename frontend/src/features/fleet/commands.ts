import type { Deployment } from '../../shared/api/types';

/** Команды ловушки (specs/frontend.md, «Команды ловушки»). */
export const COMMANDS = ['start', 'stop', 'apply_config', 'rotate_credentials', 'reset'] as const;
export type Command = (typeof COMMANDS)[number];

export type JobState = 'requested' | 'leased' | 'running' | 'succeeded' | 'failed' | 'expired';

export interface Job {
  job_id: string;
  deployment_id: string;
  command: Command;
  state: JobState;
  error: string | null;
}

export const COMMAND_LABEL: Record<Command, string> = {
  start: 'Запустить',
  stop: 'Остановить',
  apply_config: 'Применить профиль',
  rotate_credentials: 'Сменить учётки',
  reset: 'Сбросить VM',
};

/** Текст подтверждения; null — команда без подтверждения. */
export const CONFIRM: Record<Command, string | null> = {
  start: null,
  stop: 'Ловушка перестанет принимать подключения.',
  apply_config: null,
  rotate_credentials: 'Учётки-приманки сменятся; атакующий со старым паролем больше не войдёт.',
  reset: 'Вход закроется, доказательства сохранятся, среда вернётся к чистому образу.',
};

/** Какие команды можно отправить ловушке в её текущем состоянии. */
export function availableCommands(d: Pick<Deployment, 'state' | 'interaction_level' | 'desired_revision' | 'applied_revision'>): Command[] {
  const out: Command[] = [];
  const drift = d.applied_revision !== d.desired_revision;
  if (d.state === 'stopped' || d.state === 'failed' || d.state === 'planned') out.push('start');
  if (d.state === 'running') out.push('stop');
  if (d.state === 'running' && drift) out.push('apply_config');
  if (d.state === 'running' && d.interaction_level !== 'low') out.push('rotate_credentials');
  if (d.interaction_level === 'high' && (d.state === 'running' || d.state === 'failed')) out.push('reset');
  return out;
}

export const isFinal = (s: JobState) => s === 'succeeded' || s === 'failed' || s === 'expired';

/** Ход задания словами (виден, пока задание идёт, и итог). */
export function jobLabel(job: Pick<Job, 'state' | 'error'>): { tone: 'ok' | 'warn' | 'bad'; label: string } {
  switch (job.state) {
    case 'requested': return { tone: 'warn', label: 'поставлено в очередь' };
    case 'leased': return { tone: 'warn', label: 'взято агентом' };
    case 'running': return { tone: 'warn', label: 'выполняется' };
    case 'succeeded': return { tone: 'ok', label: 'выполнено' };
    case 'failed': return { tone: 'bad', label: `ошибка${job.error ? `: ${job.error}` : ''}` };
    case 'expired': return { tone: 'bad', label: 'истекло — агент не взял задание вовремя' };
    default: return { tone: 'bad', label: 'неизвестное состояние' };
  }
}
