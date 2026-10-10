// Расширение явно: модуль импортирует и vite.config.ts (nodenext).
import type { FaceKey } from './types.ts';

export type { FaceKey };

/**
 * Какая метка действует в момент time: последняя, чьё at уже наступило.
 * -1 — ни одной: действует основное выражение состояния.
 * Метки должны быть отсортированы по at.
 */
export function faceKeyAt(keys: readonly FaceKey[], time: number): number {
  let index = -1;
  for (let i = 0; i < keys.length; i++) {
    if (keys[i]!.at <= time) index = i;
    else break;
  }
  return index;
}

export const sortKeys = (keys: readonly FaceKey[]) => [...keys].sort((a, b) => a.at - b.at);
