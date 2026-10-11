import { api } from '../../shared/api/client';
import type { Host } from '../../shared/api/types';

/** Ответ `POST /api/v1/enrollments` (EnrollmentRead). */
export interface EnrollmentRead { id: string; enrollment_token: string; expires_at: string }

/** Выдать одноразовый регистрационный токен агента (только admin). */
export function issueEnrollment() {
  return api<EnrollmentRead>('/api/v1/enrollments', { method: 'POST' });
}

/** Отозвать доступ агента хоста (только admin). Ответ — хост со статусом revoked. */
export function revokeHost(id: string) {
  return api<Host>(`/api/v1/hosts/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
}

/** У отозванного хоста отзывать нечего. */
export const canRevoke = (h: Pick<Host, 'status'>) => h.status !== 'revoked';

/**
 * Выданный регистрационный токен (specs/frontend.md, «Регистрация агента»).
 * Хранится только в памяти окна; срок — по `expires_at` центра.
 */
export class EnrollmentTicket {
  readonly token: string;
  readonly expiresAt: number;

  constructor(read: EnrollmentRead) {
    this.token = read.enrollment_token;
    this.expiresAt = Date.parse(read.expires_at);
  }

  expired(now: number) { return !(this.expiresAt > now); }

  /** Сколько целых минут осталось (с округлением вверх); 0 — истёк. */
  minutesLeft(now: number) {
    return this.expired(now) ? 0 : Math.ceil((this.expiresAt - now) / 60_000);
  }
}

/** «осталось N минут» с русским согласованием. */
export function minutesLabel(n: number) {
  const mod10 = n % 10, mod100 = n % 100;
  const word = mod10 === 1 && mod100 !== 11 ? 'минута'
    : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? 'минуты' : 'минут';
  return `${n} ${word}`;
}

/**
 * Команда регистрации агента (infra/runtime/executor.md). Регистрационный
 * токен в неё не подставляется — он передаётся переменной окружения
 * HF_ENROLLMENT_TOKEN; `--token-file` — куда агент сохранит свой постоянный токен.
 */
export const ENROLL_COMMAND =
  'python -m agent.runtime.executor.enroll --center <адрес центра> --name <имя хоста> --token-file <куда сохранить токен агента>';
