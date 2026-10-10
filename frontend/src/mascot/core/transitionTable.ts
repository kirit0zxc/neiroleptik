import { parseTransitionsFile, transitionKey, type ChainStep } from './transitions';
import transitionsFile from './transitions.json';

const SAVED = parseTransitionsFile(transitionsFile);

/** Плавность переходов «из→в», мс. Сохранённые в файле плюс заданные на лету. */
export const TRANSITIONS: Record<string, number> = { ...SAVED.transitions };

/** Сохранённые в песочнице цепочки. */
export const CHAINS: Record<string, ChainStep[]> = { ...SAVED.chains };

/** Плавность перехода из from в to, если она есть в таблице. */
export function transitionFade(from: string, to: string): number | undefined {
  return TRANSITIONS[transitionKey(from, to)];
}

/**
 * Задать плавность для пары на лету, без файла:
 *   defineTransition('thinking', 'success', 600);
 */
export function defineTransition(from: string, to: string, fadeMs: number) {
  TRANSITIONS[transitionKey(from, to)] = Math.max(0, fadeMs);
}

export function removeTransition(from: string, to: string) {
  delete TRANSITIONS[transitionKey(from, to)];
}
