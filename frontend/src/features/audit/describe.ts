import { COMMAND_LABEL, driverErrorText, type Command } from '../fleet/commands';

/** Запись журнала аудита (AuditRead). */
export interface AuditEntry {
  id: string;
  actor_id: string | null;
  action: string;
  target_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

/**
 * Подписи действий центра и их результат (specs, «Журнал аудита»).
 * ok: true — успех, false — ошибка, null — сервер итог не сообщает.
 */
const ACTIONS: Readonly<Record<string, { label: string; ok: boolean }>> = {
  'auth.login': { label: 'вход в систему', ok: true },
  'auth.login_failed': { label: 'неудачный вход', ok: false },
  'auth.logout': { label: 'выход', ok: true },
  'user.created_cli': { label: 'пользователь создан (CLI)', ok: true },
  'enrollment.created': { label: 'выдан токен регистрации агента', ok: true },
  'agent.enrolled': { label: 'агент зарегистрирован', ok: true },
  'agent.revoked': { label: 'доступ агента отозван', ok: true },
  'profile.created': { label: 'профиль создан', ok: true },
  'profile.updated': { label: 'профиль изменён', ok: true },
  'profile.archived': { label: 'профиль в архиве', ok: true },
  'deployment.planned': { label: 'ловушка назначена', ok: true },
  'command.requested': { label: 'команда отправлена', ok: true },
  'command.acknowledged': { label: 'агент подтвердил команду', ok: true },
  'command.expired': { label: 'команда истекла', ok: false },
};

const ROLE: Readonly<Record<string, string>> = { admin: 'администратор', operator: 'оператор', viewer: 'наблюдатель' };

/** Подпись и результат действия; неизвестное действие — как есть, без вывода об итоге. */
export function actionInfo(e: Pick<AuditEntry, 'action' | 'details'>): { label: string; ok: boolean | null } {
  if (!Object.hasOwn(ACTIONS, e.action)) return { label: e.action, ok: null };
  const known = ACTIONS[e.action]!;
  // Итог подтверждения команды сообщает агент.
  if (e.action === 'command.acknowledged') return { label: known.label, ok: e.details.outcome !== 'failed' };
  return known;
}

const text = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v));

/** Подробности записи — строками для показа как текст (S1). */
export function describeDetails(details: Record<string, unknown>): string[] {
  return Object.entries(details).map(([key, v]) => {
    switch (key) {
      case 'revision': return `ревизия r${text(v)}`;
      case 'generation': return `поколение ${text(v)}`;
      case 'command': return `команда: ${Object.hasOwn(COMMAND_LABEL, String(v)) ? COMMAND_LABEL[v as Command].toLowerCase() : text(v)}`;
      case 'outcome': return `итог: ${v === 'succeeded' ? 'успешно' : v === 'failed' ? 'ошибка' : text(v)}`;
      case 'code': return `код: ${typeof v === 'string' ? driverErrorText(v) : text(v)}`;
      case 'role': return `роль: ${Object.hasOwn(ROLE, String(v)) ? ROLE[String(v)] : text(v)}`;
      case 'username': return `логин: ${text(v)}`;
      default: return `${key}: ${text(v)}`;
    }
  });
}

/** Сокращённый id для таблицы: первые 8 символов (полный — в подсказке). */
export const shortId = (id: string) => id.slice(0, 8);

/** Кто выполнил действие: текущий пользователь, хост (агент), система или id. */
export function whoLabel(actorId: string | null, me: { id: string; username: string } | null, hosts: ReadonlyMap<string, string>): string {
  if (actorId === null) return 'система';
  if (me && actorId === me.id) return `вы (${me.username})`;
  const host = hosts.get(actorId);
  if (host) return `хост ${host}`;
  return shortId(actorId);
}
