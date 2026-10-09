// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { ago, formatTime } from './time';

describe('время (F13)', () => {
  it('UTC переводится в заданный пояс', () => {
    const fmt = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' });
    expect(formatTime('2026-10-09T12:00:00Z', fmt)).toBe('15:00');
  });

  it('битая дата — «неизвестно», а не Invalid Date', () => {
    expect(formatTime('мусор')).toBe('неизвестно');
    expect(ago('мусор')).toBe('неизвестно');
  });

  it('относительное время', () => {
    const now = Date.parse('2026-10-09T12:00:00Z');
    expect(ago('2026-10-09T11:59:55Z', now)).toBe('5 с назад');
    expect(ago('2026-10-09T11:57:00Z', now)).toBe('3 мин назад');
    expect(ago('2026-10-09T09:00:00Z', now)).toBe('3 ч назад');
    expect(ago('2026-10-12T12:00:00Z', now)).toBe('0 с назад');
  });
});
