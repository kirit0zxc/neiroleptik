import { useEffect, useRef } from 'react';
import type { HfEvent } from '../../shared/api/types';
import { eventData, sourceIp } from '../../shared/format/source';
import { onMascotEvent, type MascotSignal } from '../../shared/mascot-slot/mascotEvents';
import { useStreamStatus } from '../../shared/stream/stream';
import type { AttackPayload } from './reactions';
import { useYumi } from './yumi';

/**
 * Поля события потока, нужные репликам Юми; остальное ей не передаём.
 * Имена полей `data` — по согласованному списку (specs/events.md): команда
 * атакующего — `session.command` → `data.input`.
 */
const HTTP_METHODS = new Set(['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH', 'TRACE', 'CONNECT']);

export function attackPayload(signal: MascotSignal, e: Pick<HfEvent, 'type' | 'source' | 'service' | 'data'>): AttackPayload {
  const data = eventData(e);
  const isCommand = signal === 'attack:command' && e.type === 'session.command';
  return {
    // Нет источника — пустой ip: правила Юми скажут «неизвестного адреса».
    ip: sourceIp(e) ?? '',
    protocol: typeof e.service?.protocol === 'string' ? e.service.protocol : '',
    username: typeof data.username === 'string' ? data.username : '',
    success: data.success === true,
    command: isCommand && typeof data.input === 'string' ? data.input : '',
    // Сервер уже урезал ввод — реплика это показывает, даже если текст короткий.
    truncated: isCommand && data.truncated === true,
    // HTTP: метод только из известного списка; путь не берём — сервер его скрывает.
    method: signal === 'attack:http' && typeof data.method === 'string' && HTTP_METHODS.has(data.method.toUpperCase())
      ? data.method.toUpperCase() : '',
    // TCP: сколько байт захвачено; отрицательное или нецелое — не называем.
    bytes: signal === 'attack:tcp' && Number.isSafeInteger(data.captured_bytes) && Number(data.captured_bytes) >= 0
      ? Number(data.captured_bytes) : null,
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
      // trap:online и alert реплик не дают; остальное отдаём react — он
      // считает нарастание и решает, показать сразу или схлопнуть.
      if (signal === 'trap:online' || signal === 'alert') return;
      yumi.react(signal, attackPayload(signal, e));
    });
  }, [yumi]);

  return null;
}
