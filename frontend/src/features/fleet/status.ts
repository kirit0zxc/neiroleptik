import type { Deployment, Host } from '../../shared/api/types';
import type { Tone } from '../../shared/ui';

/** Связь агента (F4): online — это связь, а не работа ловушки. */
export function hostStatus(h: Pick<Host, 'status'>): { tone: Tone; label: string } {
  switch (h.status) {
    case 'online': return { tone: 'ok', label: 'на связи' };
    case 'offline': return { tone: 'bad', label: 'нет связи' };
    case 'revoked': return { tone: 'bad', label: 'отозван' };
    default: return { tone: 'idle', label: 'неизвестно' };
  }
}

/**
 * Состояние ловушки (F5) в центре и демо. Работающая ловушка, у
 * которой применённая ревизия отстаёт от желаемой, — «drift», а не успех.
 */
export function deploymentStatus(d: Pick<Deployment, 'state' | 'desired_revision' | 'applied_revision'>): { tone: Tone; label: string } {
  const drift = d.applied_revision !== d.desired_revision;
  switch (d.state) {
    case 'running':
      return drift
        ? { tone: 'warn', label: `drift: r${d.applied_revision ?? '—'} → r${d.desired_revision}` }
        : { tone: 'ok', label: 'работает' };
    case 'deploying': return { tone: 'warn', label: 'разворачивается' };
    case 'stopping': return { tone: 'warn', label: 'останавливается' };
    case 'planned': return { tone: 'idle', label: 'запланирована' };
    case 'stopped': return { tone: 'idle', label: 'остановлена' };
    case 'failed': return { tone: 'bad', label: 'ошибка' };
    case 'unknown': return { tone: 'warn', label: 'состояние не подтверждено' };
    default: return { tone: 'idle', label: 'неизвестно' };
  }
}

/**
 * Хост ловушки не на связи (F5): последнее известное состояние не выдаётся за
 * текущее. Хоста нет в списке (ещё не загружен, удалён) — пометки нет.
 */
export function hostUnreachable(status: Host['status'] | undefined): boolean {
  return status !== undefined && status !== 'online';
}
