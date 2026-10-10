// Расширение явно: модуль импортирует и vite.config.ts (nodenext).
import type { StateSpec, VrmExpression } from './types.ts';
import { sortKeys, type FaceKey } from './faceTrack.ts';

/**
 * Свои состояния, собранные в студии: выражение лица + клип + свет.
 * Хранятся в presets.json рядом с этим файлом и попадают в сборку.
 *
 * Модуль без DOM: им же dev-сервер проверяет то, что сохраняет студия.
 */
export type Presets = Record<string, StateSpec>;

export const EXPRESSIONS: readonly VrmExpression[] = [
  'neutral', 'happy', 'angry', 'sad', 'relaxed', 'surprised',
];

/** Буквы, цифры, _ и -. Двойное подчёркивание в начале — служебное. */
const NAME = /^(?!__)[\p{L}\p{N}_-]{1,40}$/u;

export const isPresetName = (name: string) => NAME.test(name);

const num = (v: unknown, lo: number, hi: number, fallback: number) =>
  typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : fallback;

const COLOR = /^#[0-9a-f]{6}$/i;

/**
 * Привести что угодно к StateSpec или вернуть null.
 *
 * Файл правят руками и через студию; битая запись не должна ронять
 * маскота — она просто пропускается.
 */
export function toStateSpec(raw: unknown): StateSpec | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;

  if (!EXPRESSIONS.includes(o.expression as VrmExpression)) return null;

  const action = typeof o.action === 'string' && o.action ? o.action
    : Array.isArray(o.action) && o.action.every((a) => typeof a === 'string') && o.action.length
      ? (o.action as string[])
      : undefined;

  const reset = o.resetAfterMs === 'clip' || o.resetAfterMs === null ? o.resetAfterMs
    : typeof o.resetAfterMs === 'number' ? num(o.resetAfterMs, 100, 600_000, 3000)
      : null;

  const g = (o.glow && typeof o.glow === 'object' ? o.glow : {}) as Record<string, unknown>;
  const faceTrack = toFaceTrack(o.faceTrack);
  const trim = toTrim(o.trim);

  return {
    expression: o.expression as VrmExpression,
    weight: num(o.weight, 0, 1, 0.5),
    ...(action ? { action } : {}),
    loop: o.loop === true,
    resetAfterMs: reset,
    ...(o.speakOver === true ? { speakOver: true } : {}),
    ...(typeof o.rootMotion === 'boolean' ? { rootMotion: o.rootMotion } : {}),
    ...(o.followPointer === true ? { followPointer: true } : {}),
    ...(typeof o.fadeMs === 'number' ? { fadeMs: Math.round(num(o.fadeMs, 0, 3000, 280)) } : {}),
    ...(trim ? { trim } : {}),
    ...(faceTrack.length ? { faceTrack } : {}),
    glow: {
      color: typeof g.color === 'string' && COLOR.test(g.color) ? g.color : '#9ED8BC',
      intensity: num(g.intensity, 0, 1, 0.4),
      pulseHz: num(g.pulseHz, 0, 5, 0),
    },
  };
}

/** Обрезка: два числа, начало раньше конца хотя бы на 0.05 с. */
function toTrim(raw: unknown): [number, number] | null {
  if (!Array.isArray(raw) || raw.length !== 2) return null;
  const [a, b] = raw;
  if (typeof a !== 'number' || typeof b !== 'number' || !Number.isFinite(a) || !Number.isFinite(b)) return null;
  const start = Math.round(num(a, 0, 600, 0) * 100) / 100;
  const end = Math.round(num(b, 0, 600, 0) * 100) / 100;
  return end - start >= 0.05 ? [start, end] : null;
}

/** Метки дорожки: битые отбрасываются, остальные сортируются по времени. */
function toFaceTrack(raw: unknown): FaceKey[] {
  if (!Array.isArray(raw)) return [];
  const keys: FaceKey[] = [];
  for (const k of raw.slice(0, 64)) {
    if (!k || typeof k !== 'object') continue;
    const o = k as Record<string, unknown>;
    if (!EXPRESSIONS.includes(o.expression as VrmExpression)) continue;
    if (typeof o.at !== 'number' || !Number.isFinite(o.at)) continue;
    keys.push({
      at: Math.round(num(o.at, 0, 600, 0) * 100) / 100,
      expression: o.expression as VrmExpression,
      weight: num(o.weight, 0, 1, 0.5),
    });
  }
  return sortKeys(keys);
}

/** Разобрать весь файл: битые и неправильно названные записи отбрасываются. */
export function parsePresets(raw: unknown): Presets {
  const out: Presets = {};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  for (const [name, value] of Object.entries(raw)) {
    const spec = isPresetName(name) ? toStateSpec(value) : null;
    if (spec) out[name] = spec;
  }
  return out;
}
