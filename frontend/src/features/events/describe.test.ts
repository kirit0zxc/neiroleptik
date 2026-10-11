// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { describeEvent, eventLabel, isSecretKey, resetSecretWarning } from './describe';

describe('описание события (S1, S2, S4)', () => {
  it('пароль атакующего замаскирован', () => {
    const text = describeEvent({ type: 'auth.attempt', data: { username: 'root', password: 'hunter2', success: true } });
    expect(text).toBe('root / пароль скрыт — успешно');
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
    expect(auth({ username: 'root', password_present: true, success: true })).toBe('root / пароль скрыт — успешно');
    expect(auth({ username: 'root', password_present: false, success: false })).toBe('root / пароль не записан — отказ');
  });

  it('флага нет (старые данные) — только логин', () => {
    expect(auth({ username: 'root', success: false })).toBe('root — отказ');
  });

  afterEach(() => { vi.restoreAllMocks(); });

  it('исходный пароль не показывается ни при флаге, ни без него — ни длины, ни краёв', () => {
    resetSecretWarning();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const leaked = auth({ username: 'root', password: 'hunter2', password_present: true });
    expect(leaked).toBe('root / пароль скрыт');
    expect(leaked).not.toContain('hunter2');
    expect(leaked).not.toContain('•');
    expect(auth({ username: 'root', password: 'hunter2' })).toBe('root / пароль скрыт');
    expect(auth({ username: 'root', password: '' })).toBe('root / пароль скрыт');
    // Предупреждение одно за сессию и без значения.
    expect(warn).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(warn.mock.calls)).not.toContain('hunter2');
  });

  it('флаг не boolean — состояние неизвестно', () => {
    expect(auth({ username: 'root', password_present: 'true' })).toBe('root');
    expect(auth({ username: 'root', password_present: 1 })).toBe('root');
  });

  it('пустой логин — «(без логина)»', () => {
    expect(auth({ username: '', password_present: true, success: false })).toBe('(без логина) / пароль скрыт — отказ');
  });

  it('неизвестный тип: секреты из data не выводятся', () => {
    resetSecretWarning();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const text = describeEvent({ type: 'x.new', data: { user: 'root', password: 'hunter2', api_secret: 's', note: 'ok' } });
    expect(warn).toHaveBeenCalledTimes(1);
    expect(text).toBe('{"user":"root","note":"ok"}');
  });

  it('секретные ключи — по словам: полезные поля не прячутся, вложенные секреты убираются', () => {
    for (const k of ['password', 'user_password', 'apiSecret', 'pwd', 'api_key', 'privateKey', 'credentials']) expect(isSecretKey(k), k).toBe(true);
    for (const k of ['passive', 'bypass', 'compass', 'passenger', 'bypass_detected', 'classpath', 'token_id']) expect(isSecretKey(k), k).toBe(false);
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(describeEvent({ type: 'x.new', data: { bypass: true, auth: { user: 'root', password: 'hunter2' } } }))
      .toBe('{"bypass":true,"auth":{"user":"root"}}');
  });

  it('пароль пришёл при password_present: false — всё равно «пароль скрыт»', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(auth({ username: 'root', password: 'x', password_present: false })).toBe('root / пароль скрыт');
  });
});
