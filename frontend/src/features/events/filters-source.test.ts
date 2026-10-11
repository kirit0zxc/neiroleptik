import { describe, expect, it } from 'vitest';
import type { HfEvent } from '../../shared/api/types';
import { matches } from './filters';

const ev = (source: unknown) => ({ deployment_id: 'd', type: 'auth.attempt', occurred_at: '2026-10-11T09:00:00Z', source } as unknown as HfEvent);

describe('фильтр по IP и неизвестный источник', () => {
  it('событие без источника не подходит под фильтр по IP, без фильтра — подходит', () => {
    expect(matches(ev(null), { source_ip: '203.0.113.9' })).toBe(false);
    expect(matches(ev(null), {})).toBe(true);
    expect(matches(ev({ ip: '203.0.113.9', port: 1 }), { source_ip: '203.0.113.9' })).toBe(true);
  });
});
