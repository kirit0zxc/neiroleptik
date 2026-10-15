import type { HfEvent } from '../api/types';
import { eventData } from '../format/source';

/**
 * Адаптер «событие потока → событие маскота» (specs/frontend.md, раздел 9).
 *
 * Пока маскота нет, подписчиков нет и вызов ничего не делает. Когда Юми
 * подключат, она подпишется через `onMascotEvent` — экраны не меняются.
 */
export type MascotSignal =
  | 'trap:online' | 'trap:offline' | 'attack:connect' | 'attack:auth'
  | 'attack:command' | 'attack:http' | 'attack:tcp' | 'token:triggered' | 'alert';

const BY_TYPE: Partial<Record<string, MascotSignal>> = {
  'connection.opened': 'attack:connect',
  'auth.attempt': 'attack:auth',
  'session.command': 'attack:command',
  'http.request': 'attack:http',
  'tcp.request': 'attack:tcp',
  'token.triggered': 'token:triggered',
};

/** Какой сигнал даёт событие; null — маскоту неинтересно. */
export function mascotSignalFor(event: Pick<HfEvent, 'type' | 'data'>): MascotSignal | null {
  if (event.type === 'runtime.state') {
    const state = eventData(event).state;
    if (state === 'running') return 'trap:online';
    if (state === 'failed' || state === 'stopped') return 'trap:offline';
    return null;
  }
  return BY_TYPE[event.type] ?? null;
}

type Listener = (signal: MascotSignal, event: HfEvent) => void;
const listeners = new Set<Listener>();

export function onMascotEvent(fn: Listener) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function emitMascotEvents(events: HfEvent[]) {
  if (!listeners.size) return;
  for (const e of events) {
    const signal = mascotSignalFor(e);
    if (signal) for (const fn of listeners) fn(signal, e);
  }
}
