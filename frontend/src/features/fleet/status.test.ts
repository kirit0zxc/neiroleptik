// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { deploymentStatus, hostStatus } from './status';

describe('статусы парка (F4, F5)', () => {
  it('связь агента', () => {
    expect(hostStatus({ status: 'online' }).tone).toBe('ok');
    expect(hostStatus({ status: 'offline' }).tone).toBe('bad');
    expect(hostStatus({ status: 'revoked' })).toEqual({ tone: 'bad', label: 'отозван' });
  });

  it('работающая ловушка со старой ревизией — drift, а не успех', () => {
    const s = deploymentStatus({ state: 'running', desired_revision: 4, applied_revision: 3 });
    expect(s.tone).toBe('warn');
    expect(s.label).toBe('drift: r3 → r4');
  });

  it('ревизия ещё не применялась — тоже drift', () => {
    expect(deploymentStatus({ state: 'running', desired_revision: 1, applied_revision: null }).label).toBe('drift: r— → r1');
  });

  it('совпадающая ревизия — работает; failed — ошибка', () => {
    expect(deploymentStatus({ state: 'running', desired_revision: 2, applied_revision: 2 })).toEqual({ tone: 'ok', label: 'работает' });
    expect(deploymentStatus({ state: 'failed', desired_revision: 1, applied_revision: 1 }).tone).toBe('bad');
  });

  it('неизвестное состояние не роняет экран', () => {
    expect(deploymentStatus({ state: 'quantum' as never, desired_revision: 1, applied_revision: 1 }).label).toBe('неизвестно');
  });
});
