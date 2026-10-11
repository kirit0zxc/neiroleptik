// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { describeEvent, eventLabel } from './describe';

describe('описание события (S1, S2, S4)', () => {
  it('пароль атакующего замаскирован', () => {
    const text = describeEvent({ type: 'auth.attempt', data: { username: 'root', password: 'hunter2', success: true } });
    expect(text).toBe('root / h•••••2 успешно');
    expect(text).not.toContain('hunter2');
  });

  it('команда очищена от escape-последовательностей и помечена как обрезанная', () => {
    expect(describeEvent({ type: 'session.command', data: { input: 'ls\u001b[31m -la', truncated: true } }))
      .toBe('ls -la [обрезано]');
  });

  it('HTML в запросе остаётся строкой — его выводит React как текст', () => {
    expect(describeEvent({ type: 'http.request', data: { method: 'GET', path: '/<script>alert(1)</script>' } }))
      .toBe('GET /<script>alert(1)</script>');
  });

  it('неизвестный тип не роняет ленту', () => {
    expect(eventLabel('x.y')).toBe('Неизвестное событие (x.y)');
    expect(describeEvent({ type: 'x.y', data: { a: 1 } })).toBe('{"a":1}');
  });

  it('подключение и закрытие сессии — по сервису', () => {
    expect(describeEvent({ type: 'connection.opened', data: {}, service: { protocol: 'tcp', port: 2222 } })).toBe('tcp :2222');
    expect(describeEvent({ type: 'session.closed', data: {}, service: null })).toBe('');
  });
});
