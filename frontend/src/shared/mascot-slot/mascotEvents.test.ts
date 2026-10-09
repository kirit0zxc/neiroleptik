// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';
import type { HfEvent } from '../api/types';
import { emitMascotEvents, mascotSignalFor, onMascotEvent } from './mascotEvents';

const ev = (type: string, data: Record<string, unknown> = {}) => ({ type, data }) as HfEvent;

describe('адаптер маскота (раздел 9)', () => {
  it('типы событий переводятся в сигналы; неинтересное — null', () => {
    expect(mascotSignalFor(ev('auth.attempt'))).toBe('attack:auth');
    expect(mascotSignalFor(ev('token.triggered'))).toBe('token:triggered');
    expect(mascotSignalFor(ev('runtime.state', { state: 'failed' }))).toBe('trap:offline');
    expect(mascotSignalFor(ev('runtime.state', { state: 'deploying' }))).toBeNull();
    expect(mascotSignalFor(ev('session.output'))).toBeNull();
    expect(mascotSignalFor(ev('нечто.новое'))).toBeNull();
  });

  it('без подписчиков ничего не делает; подписчик получает сигналы и отписывается', () => {
    expect(() => emitMascotEvents([ev('auth.attempt')])).not.toThrow();
    const fn = vi.fn();
    const off = onMascotEvent(fn);
    emitMascotEvents([ev('auth.attempt'), ev('session.output')]);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0]![0]).toBe('attack:auth');
    off();
    emitMascotEvents([ev('auth.attempt')]);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
