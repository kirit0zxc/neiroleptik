import { describe, expect, it } from 'vitest';
import { serviceLabel, sourceIp, sourceLabel } from './source';

describe('источник и сервис события: неполные данные не роняют экран', () => {
  it('обычный источник', () => {
    expect(sourceLabel({ source: { ip: '203.0.113.9', port: 4000 } })).toBe('203.0.113.9:4000');
    expect(sourceIp({ source: { ip: '203.0.113.9', port: 4000 } })).toBe('203.0.113.9');
  });

  it.each([
    ['null', null], ['нет поля', undefined], ['нет ip', { port: 1 }], ['ip не строка', { ip: 42, port: 1 }],
    ['пустой ip', { ip: '', port: 1 }], ['массив', ['1.2.3.4']],
  ])('источник: %s → «источник неизвестен»', (_name, source) => {
    expect(sourceIp({ source })).toBeNull();
    expect(sourceLabel({ source })).toBe('источник неизвестен');
  });

  it('без порта — только IP', () => {
    expect(sourceLabel({ source: { ip: '2001:db8::1' } })).toBe('2001:db8::1');
  });

  it('сервис', () => {
    expect(serviceLabel({ protocol: 'tcp', port: 2222 })).toBe('tcp :2222');
    expect(serviceLabel({ protocol: 'http' })).toBe('http');
    expect(serviceLabel(null)).toBe('');
  });
});
