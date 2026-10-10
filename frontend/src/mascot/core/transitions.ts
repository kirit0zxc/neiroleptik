/**
 * Таблица переходов: плавность для конкретной пары «из → в».
 *
 * Подбирается в песочнице на вкладке «Цепочка» и хранится в
 * transitions.json рядом. Там же — сохранённые цепочки.
 *
 * Модуль без DOM: им же dev-сервер проверяет то, что сохраняет песочница.
 */

/** Один шаг цепочки. */
export interface ChainStep {
  kind: 'state' | 'clip';
  name: string;
  /** До конца клипа или фиксированное время. */
  mode: 'end' | 'secs';
  secs: number;
  /** Переход в шаг, мс; null — из таблицы, состояния или по умолчанию. */
  fadeMs: number | null;
}

export interface TransitionsFile {
  /** Ключ — «из→в», значение — плавность перехода в мс. */
  transitions: Record<string, number>;
  chains: Record<string, ChainStep[]>;
}

const ARROW = '→';
export const transitionKey = (from: string, to: string) => `${from}${ARROW}${to}`;
export const splitTransitionKey = (key: string) => key.split(ARROW) as [string, string];

const NAME = /^(?!__)[\p{L}\p{N}_ -]{1,40}$/u;
export const isChainName = (name: string) => NAME.test(name);

const ms = (v: unknown) =>
  typeof v === 'number' && Number.isFinite(v) ? Math.round(Math.min(3000, Math.max(0, v))) : null;

function toStep(raw: unknown): ChainStep | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  if (o.kind !== 'state' && o.kind !== 'clip') return null;
  if (typeof o.name !== 'string' || !o.name || o.name.length > 300) return null;
  const secs = typeof o.secs === 'number' && Number.isFinite(o.secs) ? Math.min(60, Math.max(0.5, o.secs)) : 3;
  return {
    kind: o.kind,
    name: o.name,
    mode: o.mode === 'secs' ? 'secs' : 'end',
    secs,
    fadeMs: o.fadeMs == null ? null : ms(o.fadeMs),
  };
}

/** Разобрать файл; битое отбрасывается, ничего не бросает. */
export function parseTransitionsFile(raw: unknown): TransitionsFile {
  const out: TransitionsFile = { transitions: {}, chains: {} };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  const o = raw as Record<string, unknown>;

  if (o.transitions && typeof o.transitions === 'object') {
    for (const [key, value] of Object.entries(o.transitions)) {
      const parts = key.split(ARROW);
      const fade = ms(value);
      if (parts.length === 2 && parts[0] && parts[1] && fade != null) out.transitions[key] = fade;
    }
  }
  if (o.chains && typeof o.chains === 'object') {
    for (const [name, steps] of Object.entries(o.chains)) {
      if (!isChainName(name) || !Array.isArray(steps)) continue;
      const parsed = steps.map(toStep).filter((s): s is ChainStep => s !== null).slice(0, 50);
      if (parsed.length) out.chains[name] = parsed;
    }
  }
  return out;
}
