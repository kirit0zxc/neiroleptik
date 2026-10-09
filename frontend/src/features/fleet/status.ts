import type { Host, PreviewDeployment } from '../../shared/api/types';
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
 * Состояние ловушки (F5) на экране-превью парка. Работающая ловушка, у
 * которой применённая ревизия отстаёт от желаемой, — «drift», а не успех.
 * В контракте стадии 1 есть только `planned`; остальные состояния — превью.
 */
export function deploymentStatus(d: Pick<PreviewDeployment, 'state' | 'desired_revision' | 'applied_revision'>): { tone: Tone; label: string } {
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
