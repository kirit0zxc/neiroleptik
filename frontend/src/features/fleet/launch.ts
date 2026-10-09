import type { Host } from '../../shared/api/types';
import type { Level } from '../profiles/schema';

/** Можно ли запускать на хосте (specs, «Мастер запуска», шаг 1). */
export function hostChoice(h: Pick<Host, 'status' | 'backlog' | 'disk_free_mb'>): { selectable: boolean; note: string | null } {
  if (h.status === 'offline') return { selectable: false, note: 'нет связи с агентом' };
  if (h.status === 'degraded') {
    const why = [h.backlog > 0 ? `очередь ${h.backlog}` : null, h.disk_free_mb < 2048 ? 'мало места на диске' : null]
      .filter(Boolean).join(', ');
    return { selectable: true, note: `работает с ошибками${why ? `: ${why}` : ''}` };
  }
  return { selectable: true, note: null };
}

/** Подходит ли профиль хосту; строка — причина, почему нет (шаг 2). */
export function profileFitsHost(level: Level, h: Pick<Host, 'capabilities'>): string | null {
  if (level === 'high' && !h.capabilities.includes('kvm')) return 'High нужна виртуализация KVM — на этом хосте её нет';
  return null;
}
