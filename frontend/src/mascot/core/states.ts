import type { BuiltinState, MascotState, StateSpec } from './types';
import { DANCES } from './clips';
import { parsePresets } from './presets';
import presetsFile from './presets.json';

const MINT = '#9ED8BC';
const MAGENTA = '#D6457F';
const COLD = '#6C8FD6';

/** Сколько грустить после ошибки, прежде чем вернуться в покой. */
export const ERROR_RESET_MS = 5000;

/**
 * Встроенные состояния. Словарь взят из Yumi 1.0 один в один,
 * чтобы существующий код, который шлёт эти имена, завёлся без правок.
 *
 * Поле action ссылается на имя клипа в реестре (см. clips.ts).
 * Клипа может не быть — тогда отыграет только мимика и свет.
 */
export const BUILTIN_STATES: Readonly<Record<BuiltinState, StateSpec>> = {
  idle: {
    expression: 'neutral', weight: 0,
    // Только в покое Юми смотрит на курсор: во время жестов и речи
    // взгляд в камеру — она обращается к пользователю.
    action: 'idle', loop: true, resetAfterMs: null, followPointer: true,
    glow: { color: MINT, intensity: 0.25, pulseHz: 0 },
  },
  welcome: {
    expression: 'happy', weight: 0.6,
    action: 'wave', loop: false, resetAfterMs: 3200,
    glow: { color: MINT, intensity: 0.5, pulseHz: 0 },
  },
  curious: {
    // Версия из студии стала стандартной: короткая реакция вместо осмотра.
    expression: 'surprised', weight: 0.35,
    action: 'reacting', loop: false, resetAfterMs: 1500,
    glow: { color: MINT, intensity: 0.4, pulseHz: 0 },
  },
  thinking: {
    expression: 'relaxed', weight: 0.4,
    action: 'thinking', loop: true, resetAfterMs: null,
    glow: { color: MAGENTA, intensity: 0.45, pulseHz: 0.5 },
  },
  searching: {
    expression: 'neutral', weight: 0,
    action: 'lookAround', loop: true, resetAfterMs: null,
    glow: { color: MAGENTA, intensity: 0.4, pulseHz: 0.8 },
  },
  analyzing: {
    expression: 'relaxed', weight: 0.3,
    action: 'thinking', loop: true, resetAfterMs: null,
    glow: { color: MAGENTA, intensity: 0.5, pulseHz: 1.2 },
  },
  comparing: {
    expression: 'relaxed', weight: 0.25,
    action: 'lookAround', loop: true, resetAfterMs: null,
    glow: { color: MAGENTA, intensity: 0.45, pulseHz: 0.9 },
  },
  excited: {
    expression: 'happy', weight: 0.9,
    action: 'victory', loop: false, resetAfterMs: 3000,
    glow: { color: MAGENTA, intensity: 0.8, pulseHz: 2.5 },
  },
  success: {
    expression: 'happy', weight: 0.8,
    action: 'peace', loop: false, resetAfterMs: 'clip', speakOver: true,
    glow: { color: MINT, intensity: 0.9, pulseHz: 0 },
  },
  gentle: {
    expression: 'relaxed', weight: 0.5,
    action: 'thankful', loop: false, resetAfterMs: 3000,
    glow: { color: MINT, intensity: 0.35, pulseHz: 0 },
  },
  error: {
    // Грустит недолго: одна ошибка не должна оставлять Юми грустной,
    // пока не придёт следующее событие.
    expression: 'sad', weight: 0.7,
    action: 'sad', loop: true, resetAfterMs: ERROR_RESET_MS,
    glow: { color: COLD, intensity: 0.3, pulseHz: 0 },
  },
  retry: {
    expression: 'sad', weight: 0.4,
    action: 'dismiss', loop: false, resetAfterMs: 2800,
    glow: { color: COLD, intensity: 0.4, pulseHz: 1.5 },
  },
  dance: {
    expression: 'happy', weight: 0.7,
    action: DANCES, loop: false, resetAfterMs: 'clip', speakOver: true,
    glow: { color: MAGENTA, intensity: 0.7, pulseHz: 2 },
  },
  speaking: {
    expression: 'neutral', weight: 0,
    action: 'talking', loop: true, resetAfterMs: null,
    glow: { color: MINT, intensity: 0.5, pulseHz: 0 },
  },
};

/** Свои состояния из студии (presets.json). Одноимённые перекрывают встроенные. */
export const PRESETS: Record<string, StateSpec> = Object.assign(Object.create(null), parsePresets(presetsFile));

/**
 * Все состояния, которые знает Юми: встроенные, пресеты и заданные на лету.
 *
 * Объект без прототипа: имя состояния может прийти из сети, и
 * 'constructor' или 'toString' не должны находиться как «состояния».
 */
export const STATES: Record<MascotState, StateSpec> = Object.assign(
  Object.create(null), BUILTIN_STATES, PRESETS,
);

/** Есть ли такое состояние — только своё поле, не унаследованное. */
export const hasState = (name: string) => Object.prototype.hasOwnProperty.call(STATES, name);

/**
 * Добавить или заменить состояние на лету — без правки файлов.
 *
 *   defineState('celebrate', { expression: 'happy', weight: 0.9,
 *     action: 'victory', resetAfterMs: 'clip', glow: { ... } });
 *   getMascot().setState('celebrate');
 */
export function defineState(name: string, spec: StateSpec) {
  STATES[name] = spec;
}

/**
 * Убрать своё состояние — и заданное на лету, и пресет из файла.
 * Встроенное с тем же именем возвращается.
 */
export function removeState(name: string) {
  delete PRESETS[name];
  const builtin = Object.prototype.hasOwnProperty.call(BUILTIN_STATES, name)
    ? (BUILTIN_STATES as Record<string, StateSpec>)[name]
    : undefined;
  if (builtin) STATES[name] = builtin;
  else delete STATES[name];
}
