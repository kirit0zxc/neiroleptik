// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { actionInfo, describeDetails, shortId, whoLabel } from './describe';

describe('журнал аудита: подписи (F19)', () => {
  it('все действия центра — понятной подписью и с результатом', () => {
    expect(actionInfo({ action: 'auth.login', details: {} })).toEqual({ label: 'вход в систему', ok: true });
    expect(actionInfo({ action: 'auth.login_failed', details: {} })).toEqual({ label: 'неудачный вход', ok: false });
    expect(actionInfo({ action: 'command.requested', details: {} }).label).toBe('команда отправлена');
    expect(actionInfo({ action: 'command.expired', details: {} }).ok).toBe(false);
    for (const action of ['auth.logout', 'user.created_cli', 'enrollment.created', 'agent.enrolled', 'agent.revoked',
      'profile.created', 'profile.updated', 'profile.archived', 'deployment.planned']) {
      expect(actionInfo({ action, details: {} }).label).not.toBe(action);
    }
  });

  it('подтверждение команды: результат по details.outcome', () => {
    expect(actionInfo({ action: 'command.acknowledged', details: { outcome: 'succeeded' } })).toEqual({ label: 'агент подтвердил команду', ok: true });
    expect(actionInfo({ action: 'command.acknowledged', details: { outcome: 'failed' } }).ok).toBe(false);
  });

  it('неизвестное действие — как есть, результат не утверждаем', () => {
    expect(actionInfo({ action: 'something.new', details: {} })).toEqual({ label: 'something.new', ok: null });
    expect(actionInfo({ action: 'toString', details: {} })).toEqual({ label: 'toString', ok: null });
  });

  it('подробности — текстом, известные ключи по-русски, остальные «ключ: значение»', () => {
    expect(describeDetails({ revision: 3 })).toEqual(['ревизия r3']);
    expect(describeDetails({ command: 'stop', generation: 2 })).toEqual(['команда: остановить', 'поколение 2']);
    expect(describeDetails({ outcome: 'failed', code: 'START_FAILED' })).toEqual(['итог: ошибка', 'код: не удалось запустить']);
    expect(describeDetails({ role: 'admin' })).toEqual(['роль: администратор']);
    expect(describeDetails({ username: '<img src=x onerror=alert(1)>' })).toEqual(['логин: <img src=x onerror=alert(1)>']);
    expect(describeDetails({ extra: { a: 1 } })).toEqual(['extra: {"a":1}']);
    expect(describeDetails({})).toEqual([]);
  });
});

describe('журнал аудита: кто и что', () => {
  const me = { id: 'u-me', username: 'admin' };
  const hosts = new Map([['h-1', 'local-runtime']]);

  it('сокращённый id — первые 8 символов', () => {
    expect(shortId('b8125461-0445-4218-a744-f5bab9750e93')).toBe('b8125461');
    expect(shortId('abc')).toBe('abc');
  });

  it('участник: я, хост по имени, система или сокращённый id', () => {
    expect(whoLabel('u-me', me, hosts)).toBe('вы (admin)');
    expect(whoLabel('h-1', me, hosts)).toBe('хост local-runtime');
    expect(whoLabel(null, me, hosts)).toBe('система');
    expect(whoLabel('c0ffee00-1111-4222-8333-444455556666', me, hosts)).toBe('c0ffee00');
  });
});
