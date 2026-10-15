// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { ApiError } from '../../shared/api/client';
import type { Deployment } from '../../shared/api/types';
import { availableCommands, canDelete, commandRejectText, deleteErrorText, driverErrorText, isFinal, jobLabel } from './commands';

const d = (patch: Partial<Deployment>): Pick<Deployment, 'state' | 'interaction_level'> => ({
  state: 'running', interaction_level: 'medium', ...patch,
});

describe('доступность команд (F6, specs/commands.md)', () => {
  it('start — из planned, stopped, failed', () => {
    for (const state of ['planned', 'stopped'] as const) expect(availableCommands(d({ state }))).toEqual(['start']);
    expect(availableCommands(d({ state: 'failed' }))).toEqual(['start', 'stop', 'reset']);
  });

  it('stop — из running, failed и unknown; в unknown запуск запрещён', () => {
    expect(availableCommands(d({ state: 'running' }))).toEqual(['stop', 'reset']);
    expect(availableCommands(d({ state: 'unknown' }))).toEqual(['stop']);
  });

  it('reset — Medium и High, работающей или упавшей', () => {
    expect(availableCommands(d({ interaction_level: 'high' }))).toEqual(['stop', 'reset']);
    expect(availableCommands(d({ interaction_level: 'high', state: 'failed' }))).toEqual(['start', 'stop', 'reset']);
    expect(availableCommands(d({ interaction_level: 'high', state: 'stopped' }))).toEqual(['start']);
    expect(availableCommands(d({ interaction_level: 'high', state: 'unknown' }))).toEqual(['stop']);
    expect(availableCommands(d({ interaction_level: 'medium', state: 'failed' }))).toContain('reset');
    expect(availableCommands(d({ interaction_level: 'low', state: 'running' }))).not.toContain('reset');
  });

  it('в переходных состояниях команд нет', () => {
    expect(availableCommands(d({ state: 'deploying' }))).toEqual([]);
    expect(availableCommands(d({ state: 'stopping' }))).toEqual([]);
  });
});

describe('ход задания', () => {
  it('итоговые состояния', () => {
    expect(['succeeded', 'failed', 'expired'].every((s) => isFinal(s as never))).toBe(true);
    expect(['requested', 'leased', 'running'].some((s) => isFinal(s as never))).toBe(false);
  });

  it('подписи: код драйвера важнее текста, иначе текст, иначе код как есть', () => {
    expect(jobLabel({ state: 'requested', error: null, error_code: null })).toEqual({ tone: 'warn', label: 'ожидает' });
    expect(jobLabel({ state: 'leased', error: null, error_code: null })).toEqual({ tone: 'warn', label: 'ожидает' });
    expect(jobLabel({ state: 'running', error: null, error_code: null })).toEqual({ tone: 'warn', label: 'выполняется' });
    expect(jobLabel({ state: 'succeeded', error: null, error_code: null })).toEqual({ tone: 'ok', label: 'успешно' });
    expect(jobLabel({ state: 'expired', error: null, error_code: null }).label).toBe('истёк срок — агент не выполнил команду вовремя');
    expect(jobLabel({ state: 'failed', error: 'container exited 1', error_code: 'START_FAILED' }).label).toBe('ошибка: не удалось запустить');
    expect(jobLabel({ state: 'failed', error: 'нет места на диске', error_code: 'DISK_FULL' }).label).toBe('ошибка: нет места на диске');
    expect(jobLabel({ state: 'failed', error: null, error_code: 'DISK_FULL' }).label).toBe('ошибка: DISK_FULL');
    expect(jobLabel({ state: 'failed', error: null, error_code: null }).label).toBe('ошибка');
    expect(jobLabel({ state: 'expired', error: null, error_code: null }).tone).toBe('bad');
  });

  it('коды ошибок драйвера — понятным текстом, неизвестный код как есть', () => {
    expect(driverErrorText('UNSUPPORTED_PROFILE')).toBe('профиль не поддерживается этим хостом');
    expect(driverErrorText('HEALTHCHECK_FAILED')).toBe('проверка работоспособности не прошла');
    expect(driverErrorText('START_FAILED')).toBe('не удалось запустить');
    expect(driverErrorText('BACKEND_UNAVAILABLE')).toBe('среда запуска недоступна (Docker или KVM)');
    expect(driverErrorText('CLEANUP_FAILED')).toBe('не удалось убрать после остановки');
    expect(driverErrorText('INVALID_STATE')).toBe('команда недопустима в текущем состоянии');
    expect(driverErrorText('DISK_FULL')).toBe('DISK_FULL');
    expect(driverErrorText('toString')).toBe('toString');
  });
});

describe('отказ при отправке команды (409 с кодом)', () => {
  const err = (code: string | null, detail = 'текст сервера') => new ApiError(409, detail, [], code);
  it('известные коды — понятным текстом', () => {
    expect(commandRejectText(err('STALE_GENERATION'))).toBe('Ловушку уже изменили — данные обновлены.');
    expect(commandRejectText(err('COMMAND_BUSY'))).toBe('У ловушки уже выполняется команда');
    expect(commandRejectText(err('INVALID_STATE'))).toBe('Команда недоступна в текущем состоянии');
    expect(commandRejectText(err('UNSUPPORTED_COMMAND'))).toBe('Сброс доступен для Medium и High');
    expect(commandRejectText(err('PROFILE_ARCHIVED'))).toBe('Профиль в архиве — запуск невозможен');
    expect(commandRejectText(err('UNSUPPORTED_PROFILE'))).toBe('Хост не поддерживает уровень профиля');
    expect(commandRejectText(err('AGENT_REVOKED'))).toBe('Доступ агента отозван');
    expect(commandRejectText(err('IDEMPOTENCY_CONFLICT'))).toBe('Запрос с этим ключом уже был другим — начните действие заново');
  });
  it('неизвестный код или его отсутствие — текст сервера', () => {
    expect(commandRejectText(err('SOMETHING_NEW', 'Что-то новое'))).toBe('Что-то новое');
    expect(commandRejectText(err(null, 'Без кода'))).toBe('Без кода');
    expect(commandRejectText(err('toString', 'Прототип'))).toBe('Прототип');
    expect(commandRejectText(new Error('сеть'))).toBe('Команда не отправлена');
  });
});

describe('удаление ловушки (F6a)', () => {
  it('canDelete — только stopped', () => {
    for (const state of ['stopped'] as const) expect(canDelete({ state })).toBe(true);
    for (const state of ['failed', 'planned', 'deploying', 'running', 'stopping', 'unknown'] as const) expect(canDelete({ state })).toBe(false);
  });

  it('deleteErrorText — 409 показывает detail, неизвестный результат просит повторить', () => {
    expect(deleteErrorText(new ApiError(409, 'Состояние исполнителя не подтверждено', [], 'DELETE_NOT_ALLOWED')))
      .toBe('Состояние исполнителя не подтверждено');
    // Сеть/обрыв (0) и 5xx — результат неизвестен.
    expect(deleteErrorText(new ApiError(0, 'Нет связи с сервером'))).toBe('Нет связи с сервером — результат неизвестен, повторите');
    expect(deleteErrorText(new ApiError(503, 'Ошибка сервера — попробуйте позже'))).toBe('Ошибка сервера — попробуйте позже — результат неизвестен, повторите');
    expect(deleteErrorText(new Error('х'))).toBe('Не удалось удалить ловушку');
  });
});
