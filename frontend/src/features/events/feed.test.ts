// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import type { HfEvent } from '../../shared/api/types';
import { FeedBuffer } from './FeedBuffer';
import { filtersFromSearch, filtersToSearch, isIp, matches, toQuery, validateFilters } from './filters';

const ev = (id: string, patch: Partial<HfEvent> = {}): HfEvent => ({
  event_id: id, agent_id: 'a', deployment_id: 'decoy-low-01', instance_id: null, session_id: null,
  occurred_at: '2026-10-09T12:00:00Z', received_at: '2026-10-09T12:00:01Z', type: 'http.request', source: { ip: '192.0.2.1', port: 1 },
  service: { protocol: 'http', port: 80 }, data: {}, artifact_ids: [], profile_revision: 1, ...patch,
});

describe('фильтры ленты (F11)', () => {
  it('адрес страницы ↔ фильтры, пустые значения отбрасываются', () => {
    const f = filtersFromSearch(new URLSearchParams('type=auth.attempt&source_ip=%20&junk=1'));
    expect(f).toEqual({ type: 'auth.attempt' });
    expect(filtersToSearch({ type: 'auth.attempt', deployment_id: 'd1' }).toString()).toBe('deployment_id=d1&type=auth.attempt');
  });

  it('IP: v4 и v6 принимаются, мусор — нет', () => {
    for (const ok of ['192.0.2.23', '2001:db8::1', '::1']) expect(isIp(ok)).toBe(true);
    for (const bad of ['256.1.1.1', '1.2.3', 'example.com', '1::2::3', '']) expect(isIp(bad)).toBe(false);
  });

  it('ошибки: неверный IP и «до» раньше «с»', () => {
    expect(validateFilters({ source_ip: 'abc', from: '2026-10-09T12:00', to: '2026-10-09T11:00' }))
      .toEqual({ source_ip: 'Нужен IPv4 или IPv6', to: '«До» должно быть позже «с»' });
    // to исключительно: при from = to выборка пустая.
    expect(validateFilters({ from: '2026-10-09T12:00', to: '2026-10-09T12:00' })).toEqual({ to: '«До» должно быть позже «с»' });
    expect(validateFilters({ source_ip: '192.0.2.1', from: '2026-10-09T11:00', to: '2026-10-09T12:00' })).toEqual({});
  });

  it('в запрос время уходит в UTC, плюс курсор и лимит', () => {
    const local = '2026-10-09T15:00';
    const q = toQuery({ from: local, type: 'session.command' }, 'c2');
    expect(q.get('from')).toBe(new Date(local).toISOString());
    expect(q.get('cursor')).toBe('c2');
    expect(q.get('limit')).toBe('50');
  });

  it('живое событие проходит те же фильтры', () => {
    const e = ev('1', { type: 'auth.attempt', source: { ip: '192.0.2.23', port: 1 } });
    expect(matches(e, {})).toBe(true);
    expect(matches(e, { type: 'auth.attempt', source_ip: '192.0.2.23' })).toBe(true);
    expect(matches(e, { type: 'session.command' })).toBe(false);
    expect(matches(e, { deployment_id: 'other' })).toBe(false);
    expect(matches(e, { to: '2000-01-01T00:00' })).toBe(false);
    // from включительно, to исключительно — событие ровно на границе.
    const at = new Date('2026-10-09T12:00:00Z');
    const local = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    expect(matches(e, { from: local(at) })).toBe(true);
    expect(matches(e, { to: local(at) })).toBe(false);
  });
});

describe('окно ленты (FeedBuffer)', () => {
  it('живые сверху, повтор из REST и потока — один раз', () => {
    const b = new FeedBuffer();
    b.loadPage([ev('2'), ev('1')], false);
    b.pushLive([ev('3'), ev('2'), ev('4')]);
    expect(b.events.map((e) => e.event_id)).toEqual(['4', '3', '2', '1']);
  });

  it('пауза копит новые в счётчике, «показать» выводит их сверху', () => {
    const b = new FeedBuffer();
    b.loadPage([ev('1')], false);
    b.setPaused(true);
    b.pushLive([ev('2')]);
    b.pushLive([ev('3'), ev('2')]);
    expect(b.pending).toBe(2);
    expect(b.events.map((e) => e.event_id)).toEqual(['1']);
    b.flush();
    expect(b.events.map((e) => e.event_id)).toEqual(['3', '2', '1']);
    expect(b.isPaused).toBe(true);
  });

  it('окно ограничено, вытесненное можно догрузить снова', () => {
    const b = new FeedBuffer(3);
    b.loadPage([ev('3'), ev('2'), ev('1')], false);
    b.pushLive([ev('4')]);
    expect(b.events.map((e) => e.event_id)).toEqual(['4', '3', '2']);
    b.loadPage([ev('1')], true);
    expect(b.events.map((e) => e.event_id)).toEqual(['4', '3', '2']);
  });

  it('следующая страница дописывается снизу; новая выборка заменяет окно', () => {
    const b = new FeedBuffer();
    b.loadPage([ev('3'), ev('2')], false);
    b.loadPage([ev('2'), ev('1')], true);
    expect(b.events.map((e) => e.event_id)).toEqual(['3', '2', '1']);
    b.loadPage([ev('9')], false);
    expect(b.events.map((e) => e.event_id)).toEqual(['9']);
  });
});
