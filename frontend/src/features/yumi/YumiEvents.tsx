import { useEffect, useRef } from 'react';
import type { HfEvent } from '../../shared/api/types';
import { onMascotEvent, type MascotSignal } from '../../shared/mascot-slot/mascotEvents';
import { useStreamStatus } from '../../shared/stream/stream';
import { useYumi } from './yumi';

/**
 * Поля события потока, нужные репликам Юми; остальное ей не передаём.
 * Имена полей `data` — по согласованному списку (specs/events.md): команда
 * атакующего — `session.command` → `data.input`.
 */
export function attackPayload(signal: MascotSignal, e: Pick<HfEvent, 'source' | 'service' | 'data'>) {
  const data = e.data;
  return {
    ip: e.source.ip,
    protocol: e.service.protocol,
    username: typeof data.username === 'string' ? data.username : '',
    success: data.success === true,
    command: signal === 'attack:command' && typeof data.input === 'string' ? data.input : '',
  };
}

/**
 * Связывает оболочку и поток с Юми (specs/frontend.md, раздел 9): приветствие
 * после входа, потеря и восстановление связи с потоком, события атак.
 * Ничего не рисует.
 */
export function YumiEvents({ username }: { username: string }) {
  const yumi = useYumi();
  const status = useStreamStatus();
  const lastStatus = useRef(status);

  // Приветствие — один раз на пользователя (StrictMode запускает эффект дважды).
  const greeted = useRef<string | null>(null);
  useEffect(() => {
    if (!yumi || greeted.current === username) return;
    greeted.current = username;
    yumi.handle('app:welcome', { username });
  }, [yumi, username]);

  // Потерю связи объявляем только после того, как поток уже работал.
  useEffect(() => {
    const prev = lastStatus.current;
    lastStatus.current = status;
    if (!yumi || prev === status) return;
    if (prev === 'live' && status === 'reconnecting') yumi.handle('stream:lost');
    if (prev === 'reconnecting' && status === 'live') yumi.handle('stream:restored');
  }, [yumi, status]);

  useEffect(() => {
    if (!yumi) return;
    return onMascotEvent((signal, e) => {
      // trap:online и alert реплик не дают; медовый токен и остановка ловушки
      // важнее шторма подключений — идут без схлопывания.
      if (signal === 'trap:online' || signal === 'alert') return;
      if (signal === 'trap:offline' || signal === 'token:triggered') yumi.handle(signal, attackPayload(signal, e));
      else yumi.attack(signal, attackPayload(signal, e));
    });
  }, [yumi]);

  return null;
}
