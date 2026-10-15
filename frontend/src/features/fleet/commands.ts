import { api, ApiError } from '../../shared/api/client';
import type { Deployment } from '../../shared/api/types';

/**
 * Команды ловушки (specs/frontend.md, «Команды ловушки»; specs/commands.md).
 * Центр выполняет start/stop/reset; apply_config и rotate_credentials на
 * сервере не реализованы и в UI не показываются.
 */
export const COMMANDS = ['start', 'stop', 'reset'] as const;
export type Command = (typeof COMMANDS)[number];

export type JobState = 'requested' | 'leased' | 'running' | 'succeeded' | 'failed' | 'expired';

/** Задание команды (JobRead). */
export interface Job {
  job_id: string;
  deployment_id: string;
  command: Command;
  state: JobState;
  error: string | null;
  /** Код ошибки драйвера или центра — по нему выбираем понятный текст. */
  error_code: string | null;
  generation: number;
  created_at: string;
  completed_at: string | null;
}

export const COMMAND_LABEL: Record<Command, string> = {
  start: 'Запустить',
  stop: 'Остановить',
  reset: 'Сбросить ловушку',
};

/** Текст подтверждения; null — команда без подтверждения. */
export const CONFIRM: Record<Command, string | null> = {
  start: null,
  stop: 'Ловушка перестанет принимать подключения.',
  reset: 'Вход закроется, доказательства сохранятся, среда вернётся к чистому образу.',
};

/** Какие команды центр примет в текущем состоянии (specs/commands.md). */
export function availableCommands(d: Pick<Deployment, 'state' | 'interaction_level'>): Command[] {
  const out: Command[] = [];
  if (d.state === 'planned' || d.state === 'stopped' || d.state === 'failed') out.push('start');
  if (d.state === 'running' || d.state === 'failed' || d.state === 'unknown') out.push('stop');
  if (['medium', 'high'].includes(d.interaction_level) && (d.state === 'running' || d.state === 'failed')) out.push('reset');
  return out;
}

export const isFinal = (s: JobState) => s === 'succeeded' || s === 'failed' || s === 'expired';

/** Коды ошибок драйвера ловушки (участник 3) — понятным текстом (specs, «Команды ловушки»). */
const DRIVER_ERRORS: Readonly<Record<string, string>> = {
  UNSUPPORTED_PROFILE: 'профиль не поддерживается этим хостом',
  HEALTHCHECK_FAILED: 'проверка работоспособности не прошла',
  START_FAILED: 'не удалось запустить',
  BACKEND_UNAVAILABLE: 'среда запуска недоступна (Docker или KVM)',
  CLEANUP_FAILED: 'не удалось убрать после остановки',
  INVALID_STATE: 'команда недопустима в текущем состоянии',
};

/** Текст ошибки задания: известный код драйвера переводим, остальное показываем как есть. */
export function driverErrorText(error: string): string {
  return Object.hasOwn(DRIVER_ERRORS, error) ? DRIVER_ERRORS[error]! : error;
}

/** Причина неудачи задания: известный код, иначе текст центра, иначе сам код. */
export function failureText(job: Pick<Job, 'error' | 'error_code'>): string | null {
  if (job.error_code && Object.hasOwn(DRIVER_ERRORS, job.error_code)) return DRIVER_ERRORS[job.error_code]!;
  return job.error || job.error_code || null;
}

/** Ход задания словами (виден, пока задание идёт, и итог). */
export function jobLabel(job: Pick<Job, 'state' | 'error' | 'error_code'>): { tone: 'ok' | 'warn' | 'bad'; label: string } {
  switch (job.state) {
    // Принятие команды ещё не значит, что ловушка запустилась.
    case 'requested':
    case 'leased': return { tone: 'warn', label: 'ожидает' };
    case 'running': return { tone: 'warn', label: 'выполняется' };
    case 'succeeded': return { tone: 'ok', label: 'успешно' };
    case 'failed': {
      const why = failureText(job);
      return { tone: 'bad', label: why ? `ошибка: ${why}` : 'ошибка' };
    }
    case 'expired': return { tone: 'bad', label: 'истёк срок — агент не выполнил команду вовремя' };
    default: return { tone: 'bad', label: 'неизвестное состояние' };
  }
}

/** Отказы центра при отправке команды (409, `ErrorResponse.code`). */
const REJECTS: Readonly<Record<string, string>> = {
  STALE_GENERATION: 'Ловушку уже изменили — данные обновлены.',
  COMMAND_BUSY: 'У ловушки уже выполняется команда',
  INVALID_STATE: 'Команда недоступна в текущем состоянии',
  UNSUPPORTED_COMMAND: 'Сброс доступен для Medium и High',
  PROFILE_ARCHIVED: 'Профиль в архиве — запуск невозможен',
  UNSUPPORTED_PROFILE: 'Хост не поддерживает уровень профиля',
  AGENT_REVOKED: 'Доступ агента отозван',
  IDEMPOTENCY_CONFLICT: 'Запрос с этим ключом уже был другим — начните действие заново',
};

/** Сообщение об отказе: известный код — понятным текстом, иначе текст сервера. */
export function commandRejectText(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Команда не отправлена';
  return error.code && Object.hasOwn(REJECTS, error.code) ? REJECTS[error.code]! : error.message;
}

/**
 * Удаление ловушки доступно (F6a) только из stopped: сервер дополнительно проверяет ACK stop; тогда
 * исполнитель точно не держит порт. В других состояниях сервер всё равно
 * откажет (409) — кнопку просто не показываем.
 */
export function canDelete(d: Pick<Deployment, 'state'>): boolean {
  return d.state === 'stopped';
}

/**
 * Удалить ловушку (F6a). 204 и 404 — успех (ловушки больше нет), резерв порта
 * освобождён. 409 и прочее — пробрасываем `ApiError`, чтобы показать `detail`.
 */
export async function deleteDeployment(id: string): Promise<void> {
  try {
    await api<void>(`/api/v1/deployments/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return;
    throw error;
  }
}

/**
 * Сообщение об отказе удаления: 404 — ловушки уже нет (не ошибка, вызывающий
 * трактует как успех), иначе текст `detail` сервера (409 и прочее), а при
 * неизвестном результате (сеть/обрыв/5xx) — что запрос мог не дойти.
 */
export function deleteErrorText(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Не удалось удалить ловушку';
  // Сеть/обрыв (0) и 5xx — запрос мог дойти; тот же критерий, что в operations.ts и useJob.ts.
  if (error.status === 0 || error.status >= 500) return `${error.message} — результат неизвестен, повторите`;
  return error.message;
}
