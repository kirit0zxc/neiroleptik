// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { hostChoice, profileFitsHost } from './launch';

describe('мастер запуска: выбор (F7)', () => {
  it('online — можно; offline и revoked — нельзя, с причиной', () => {
    expect(hostChoice({ status: 'online' })).toEqual({ selectable: true, note: null });
    expect(hostChoice({ status: 'offline' })).toEqual({ selectable: false, note: 'нет связи с агентом' });
    expect(hostChoice({ status: 'revoked' })).toEqual({ selectable: false, note: 'доступ агента отозван' });
  });

  it('High — только на хост с KVM', () => {
    expect(profileFitsHost('high', { capabilities: ['docker'] })).toMatch(/KVM/);
    expect(profileFitsHost('high', { capabilities: ['docker', 'kvm'] })).toBeNull();
    expect(profileFitsHost('medium', { capabilities: ['docker'] })).toBeNull();
  });
});
