import type { Host } from '../../shared/api/types';
import type { Level } from '../profiles/schema';

/** Можно ли запускать на хосте (specs, «Мастер запуска», шаг 1). */
export function hostChoice(h: Pick<Host, 'status'>): { selectable: boolean; note: string | null } {
  if (h.status === 'offline') return { selectable: false, note: 'нет связи с агентом' };
  if (h.status === 'revoked') return { selectable: false, note: 'доступ агента отозван' };
  return { selectable: true, note: null };
}

/** Подходит ли профиль хосту; строка — причина, почему нет (шаг 2). */
export function profileFitsHost(level: Level, h: Pick<Host, 'capabilities'>): string | null {
  if (level === 'high' && !h.capabilities.includes('kvm')) return 'High нужна виртуализация KVM — на этом хосте её нет';
  return null;
}
