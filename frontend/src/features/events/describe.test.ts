// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';
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
});

describe('пароль: маска сервера (S4, согласовано 11.10)', () => {
  const auth = (data: Record<string, unknown>) => describeEvent({ type: 'auth.attempt', data });

  it('password_present: true — «пароль скрыт»; false — «пароль не записан»', () => {
    expect(auth({ username: 'root', password_present: true, success: true })).toBe('root / пароль скрыт успешно');
    expect(auth({ username: 'root', password_present: false, success: false })).toBe('root / пароль не записан отказ');
  });

  it('флага нет (старые данные) — только логин', () => {
    expect(auth({ username: 'root', success: false })).toBe('root отказ');
  });

  it('исходный пароль при флаге не показывается; без флага — своя маска', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const leaked = auth({ username: 'root', password: 'hunter2', password_present: true });
    expect(leaked).toBe('root / пароль скрыт');
    expect(leaked).not.toContain('hunter2');
    expect(leaked).not.toContain('•');
    expect(auth({ username: 'root', password: 'hunter2' })).toBe('root / h•••••2');
    expect(auth({ username: 'root', password: '' })).toBe('root / (пусто)');
    warn.mockRestore();
  });
});
