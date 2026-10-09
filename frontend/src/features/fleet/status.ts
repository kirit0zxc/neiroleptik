import type { Deployment, Host } from '../../shared/api/types';
import type { Tone } from '../../shared/ui';

/** Связь агента (F4): online — это связь, а не работа ловушки. */
export function hostStatus(h: Pick<Host, 'status'>): { tone: Tone; label: string } {
  switch (h.status) {
    case 'online': return { tone: 'ok', label: 'на связи' };
    case 'degraded': return { tone: 'warn', label: 'с ошибками' };
    case 'offline': return { tone: 'bad', label: 'нет связи' };
    default: return { tone: 'idle', label: 'неизвестно' };
  }
}

/**
 * Состояние ловушки (F5). Работающая ловушка, у которой применённая
 * ревизия отстаёт от желаемой, — «drift», а не зелёный успех.
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
    default: return { tone: 'idle', label: 'неизвестно' };
  }
}
