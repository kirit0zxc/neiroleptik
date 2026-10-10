import { describe, expect, it } from 'vitest';
import { formatOffset, ReplayClock } from './clock';

describe('Replay: часы проигрывателя (F15)', () => {
  it('пуск, скорость и пауза', () => {
    const c = new ReplayClock();
    c.play(0, 10_000);
    expect(c.position(1000, 10_000)).toBe(1000);
    c.setSpeed(4, 1000, 10_000);
    expect(c.position(2000, 10_000)).toBe(5000);
    c.pause(2000, 10_000);
    expect(c.position(9000, 10_000)).toBe(5000);
    expect(c.playing).toBe(false);
  });

  it('перемотка ограничена записью и не останавливает пуск', () => {
    const c = new ReplayClock();
    c.play(0, 10_000);
    c.seek(99_000, 500, 10_000);
    expect(c.position(500, 10_000)).toBe(10_000);
    c.seek(-5, 500, 10_000);
    expect(c.position(1500, 10_000)).toBe(1000);
  });

  it('на конце — пауза; пуск из конца начинает сначала', () => {
    const c = new ReplayClock();
    c.play(0, 1000);
    c.tick(2000, 1000);
    expect(c.playing).toBe(false);
    expect(c.position(5000, 1000)).toBe(1000);
    c.play(5000, 1000);
    expect(c.position(5100, 1000)).toBe(100);
  });

  it('мм:сс', () => {
    expect(formatOffset(0)).toBe('00:00');
    expect(formatOffset(65_900)).toBe('01:05');
    expect(formatOffset(6_000_000)).toBe('100:00');
  });
});
