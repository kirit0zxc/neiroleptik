import type { Host } from '../../shared/api/types';
import type { Level } from '../profiles/schema';

/** Можно ли запускать на хосте (specs, «Мастер запуска», шаг 1). */
export function hostChoice(h: Pick<Host, 'status'>): { selectable: boolean; note: string | null } {
  if (h.status === 'offline') return { selectable: false, note: 'нет связи с агентом' };
  if (h.status === 'revoked') return { selectable: false, note: 'доступ агента отозван' };
  return { selectable: true, note: null };
}

/**
 * Подходит ли профиль хосту; строка — причина, почему нет (шаг 2).
 * По контракту `HostRead.capabilities` — это поддерживаемые уровни
 * взаимодействия (`low`/`medium`/`high`): хост годится, если умеет уровень профиля.
 */
export function profileFitsHost(level: Level, h: Pick<Host, 'capabilities'>): string | null {
  if (!h.capabilities.includes(level)) {
    return level === 'high'
      ? 'High нужна поддержка настоящей VM — на этом хосте её нет'
      : `Хост не поддерживает уровень ${level}`;
  }
  return null;
}
