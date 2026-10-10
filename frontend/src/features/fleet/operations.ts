import { createContext, useContext, useSyncExternalStore } from 'react';
import { api, ApiError } from '../../shared/api/client';
import { commandRejectText, type Command } from './commands';

/**
 * Действие пользователя: команда, ключ идемпотентности и поколение, по
 * которому он видел ловушку. Ключ создаётся один раз на действие и
 * повторяется при повторной отправке после сбоя (specs, «Команды ловушки»).
 */
export interface Action { command: Command; key: string; generation: number }

/** Текущая операция ловушки: действие, задание, ход отправки и её ошибка. */
export interface Operation {
  action: Action | null;
  jobId: string | null;
  sending: boolean;
  error: unknown;
}

const IDLE: Operation = { action: null, jobId: null, sending: false, error: null };

/**
 * Операции с ловушками на уровне приложения (specs, «Команды ловушки»).
 * Живут вне строк таблицы: ни сбой обновления списка, ни исчезновение
 * строки, ни уход со страницы не теряют задание и ключ повтора. Отправка
 * завершается в хранилище, даже если строка в этот момент размонтирована.
 */
export class Operations {
  private readonly ops = new Map<string, Operation>();
  private readonly listeners = new Set<() => void>();
  /** Перечитать данные ловушек (после отказа центра или отмены). */
  private readonly refresh: () => void;
  /** Сообщить о ходе операции (Юми, раздел 9). */
  private readonly notify: (event: string, payload: unknown) => void;

  constructor(refresh: () => void = () => {}, notify: (event: string, payload: unknown) => void = () => {}) {
    this.refresh = refresh;
    this.notify = notify;
  }

  get(id: string): Operation { return this.ops.get(id) ?? IDLE; }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  /** Новое действие: новый ключ, поколение — из актуальной ловушки. */
  begin(id: string, command: Command, generation: number) {
    const action: Action = { command, key: crypto.randomUUID(), generation };
    this.set(id, { action, jobId: null, error: null });
    void this.send(id, action);
  }

  /** Повтор того же действия с тем же ключом (запрос мог дойти). */
  retry(id: string) {
    const { action } = this.get(id);
    if (action) void this.send(id, action);
  }

  /** Задание завершилось: следующее нажатие будет новым действием. */
  finish(id: string) { if (this.get(id).action) this.set(id, { action: null }); }

  /** Задания, об итоге которых уже сказано (живут дольше строки таблицы). */
  private readonly reported = new Set<string>();

  /** true — об итоге задания ещё не сообщали; сообщить можно один раз. */
  reportOnce(jobId: string): boolean {
    if (this.reported.has(jobId)) return false;
    this.reported.add(jobId);
    return true;
  }

  /** Отказ от неотправленного действия (постоянный сбой): блокировка снимается. */
  cancel(id: string) {
    this.set(id, { action: null, error: null });
    // Команда могла дойти: парк покажет её состояние.
    this.refresh();
  }

  private async send(id: string, action: Action) {
    this.set(id, { sending: true, error: null });
    try {
      const r = await api<{ job_id: string }>(`/api/v1/deployments/${encodeURIComponent(id)}/commands`, {
        method: 'POST',
        body: JSON.stringify({ command: action.command, idempotency_key: action.key, expected_generation: action.generation }),
      });
      // 202 без job_id (например, пустое тело): команда могла быть принята — результат неизвестен.
      if (!r?.job_id) throw new ApiError(0, 'Ответ сервера не распознан — результат неизвестен');
      // Поздний ответ на уже отменённое или заменённое действие не трогает текущее.
      if (this.get(id).action !== action) return;
      this.set(id, { sending: false, jobId: r.job_id });
      // Центр принял команду (202) — это ещё не запуск: ждём агента.
      this.notify('command:sent', { command: action.command });
    } catch (error) {
      if (this.get(id).action !== action) return;
      this.set(id, { sending: false, error });
      // Запрос мог дойти, ответ потерян: результат неизвестен.
      if (error instanceof ApiError && (error.status === 0 || error.status >= 500)) this.notify('command:unknown', { command: action.command });
      else this.notify('command:rejected', { command: action.command, reason: commandRejectText(error) });
      if (error instanceof ApiError && error.status === 409) this.refresh();
    }
  }

  private set(id: string, patch: Partial<Operation>) {
    this.ops.set(id, { ...this.get(id), ...patch });
    for (const l of this.listeners) l();
  }
}

export const OperationsContext = createContext<Operations | null>(null);

/** Операция ловушки и хранилище; вне OperationsContext — ошибка сборки экрана. */
export function useOperation(id: string): [Operation, Operations] {
  const ops = useContext(OperationsContext);
  if (!ops) throw new Error('useOperation вне OperationsContext');
  const op = useSyncExternalStore(ops.subscribe, () => ops.get(id));
  return [op, ops];
}
