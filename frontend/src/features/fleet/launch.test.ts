// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { hostChoice, profileFitsHost } from './launch';

describe('мастер запуска: выбор (F7)', () => {
  it('online — можно; offline и revoked — нельзя, с причиной', () => {
    expect(hostChoice({ status: 'online' })).toEqual({ selectable: true, note: null });
    expect(hostChoice({ status: 'offline' })).toEqual({ selectable: false, note: 'нет связи с агентом' });
    expect(hostChoice({ status: 'revoked' })).toEqual({ selectable: false, note: 'доступ агента отозван' });
  });

  it('профиль требует поддержки своего уровня хостом (HostRead.capabilities)', () => {
    // capabilities по контракту — поддерживаемые уровни взаимодействия.
    expect(profileFitsHost('high', { capabilities: ['low', 'medium'] })).toMatch(/VM/);
    expect(profileFitsHost('high', { capabilities: ['low', 'medium', 'high'] })).toBeNull();
    expect(profileFitsHost('medium', { capabilities: ['low', 'medium'] })).toBeNull();
    expect(profileFitsHost('medium', { capabilities: ['low'] })).toBe('Хост не поддерживает уровень medium');
  });
});
