// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { hostChoice, profileFitsHost } from './launch';

describe('мастер запуска: выбор (F7)', () => {
  it('online — можно; offline — нельзя; degraded — можно с причиной', () => {
    expect(hostChoice({ status: 'online', backlog: 0, disk_free_mb: 9000 })).toEqual({ selectable: true, note: null });
    expect(hostChoice({ status: 'offline', backlog: 0, disk_free_mb: 9000 }).selectable).toBe(false);
    expect(hostChoice({ status: 'degraded', backlog: 340, disk_free_mb: 900 }))
      .toEqual({ selectable: true, note: 'работает с ошибками: очередь 340, мало места на диске' });
    expect(hostChoice({ status: 'degraded', backlog: 0, disk_free_mb: 9000 }).note).toBe('работает с ошибками');
  });

  it('High — только на хост с KVM', () => {
    expect(profileFitsHost('high', { capabilities: ['docker'] })).toMatch(/KVM/);
    expect(profileFitsHost('high', { capabilities: ['docker', 'kvm'] })).toBeNull();
    expect(profileFitsHost('medium', { capabilities: ['docker'] })).toBeNull();
  });
});
