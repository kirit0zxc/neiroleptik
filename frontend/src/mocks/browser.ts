import { setupWorker } from 'msw/browser';
import { handlers } from './handlers';
import { nextDemoEvent, publish } from './stream';

export const worker = setupWorker(...handlers);

/** Демо-атаки в режиме моков: событие каждые несколько секунд. */
export function startDemoAttacks(intervalMs = 4000) {
  return setInterval(() => publish(nextDemoEvent()), intervalMs);
}
