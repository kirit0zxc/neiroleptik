import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { api, notifyUnauthorized } from '../api/client';
import type { HfEvent } from '../api/types';
import { emitMascotEvents } from '../mascot-slot/mascotEvents';
import { StreamClient, type GapPage, type StreamRecord, type StreamSocket, type StreamStatus } from './StreamClient';

/**
 * Живой поток событий для экранов (specs, раздел 5). Один клиент на
 * сессию оператора: создаётся при входе, закрывается при выходе.
 */
export type EventStream = StreamClient<HfEvent>;

/** Настоящий поток: WebSocket того же origin + догрузка по REST. */
export function createEventStream(): EventStream {
  return new StreamClient<HfEvent>({
    connect: () => {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      return wrapWebSocket(new WebSocket(`${proto}//${location.host}/api/v1/stream`));
    },
    fetchGap: (after, until, page) => {
      const q = new URLSearchParams({ after, until, limit: '100' });
      if (page) q.set('cursor', page);
      return api<GapPage<HfEvent>>(`/api/v1/stream-events?${q}`);
    },
    // 4401 — сессия истекла; 1006 и прочее — проверить сессию: при 401 api() сам уведёт на вход.
    onUnauthorized: notifyUnauthorized,
    onAbnormalClose: () => { void api('/api/v1/auth/me').catch(() => {}); },
  });
}

/** Браузерный WebSocket → узкий интерфейс StreamSocket (только нужные поля). */
function wrapWebSocket(ws: WebSocket): StreamSocket {
  const s: StreamSocket = { onopen: null, onmessage: null, onclose: null, onerror: null, close: () => ws.close() };
  ws.onopen = () => s.onopen?.();
  ws.onmessage = (ev) => { if (typeof ev.data === 'string') s.onmessage?.({ data: ev.data }); };
  ws.onclose = (ev) => s.onclose?.({ code: ev.code });
  ws.onerror = () => s.onerror?.();
  return s;
}

const StreamContext = createContext<EventStream | null>(null);

export function StreamProvider({ create, children }: { create: () => EventStream; children: ReactNode }) {
  const [stream] = useState(create);
  useEffect(() => {
    // Маскоту — все события потока (пока подписчиков нет, это пустой вызов).
    const off = stream.subscribe((rs) => emitMascotEvents(rs.map((r) => r.event)));
    stream.start();
    return () => { off(); stream.stop(); };
  }, [stream]);
  return <StreamContext.Provider value={stream}>{children}</StreamContext.Provider>;
}

/** Подписка на новые события потока. */
export function useStreamEvents(fn: (events: HfEvent[]) => void) {
  const stream = useContext(StreamContext);
  useEffect(() => {
    if (!stream) return;
    return stream.subscribe((rs: StreamRecord<HfEvent>[]) => fn(rs.map((r) => r.event)));
  }, [stream, fn]);
}

/** Состояние потока для индикатора. */
export function useStreamStatus(): StreamStatus {
  const stream = useContext(StreamContext);
  const [status, setStatus] = useState<StreamStatus>(stream?.currentStatus ?? 'idle');
  useEffect(() => {
    if (!stream) return;
    setStatus(stream.currentStatus);
    return stream.onStatus(setStatus);
  }, [stream]);
  return status;
}

const LABEL: Record<StreamStatus, string> = {
  idle: 'нет потока', connecting: 'подключение…', live: 'онлайн', reconnecting: 'переподключение…',
  forbidden: 'нет доступа к потоку', stopped: 'нет потока',
};
const TONE: Record<StreamStatus, 'ok' | 'warn' | 'bad' | 'idle'> = {
  idle: 'idle', connecting: 'warn', live: 'ok', reconnecting: 'warn', forbidden: 'bad', stopped: 'bad',
};

export function streamLabel(s: StreamStatus) { return { label: LABEL[s], tone: TONE[s] }; }
