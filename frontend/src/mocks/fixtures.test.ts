// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import events from '../../../contracts/fixtures/events.draft.json';
import hosts from '../../../contracts/fixtures/hosts.draft.json';
import profiles from '../../../contracts/fixtures/profiles.draft.json';
import me from '../../../contracts/fixtures/me.draft.json';

/**
 * Инварианты фикстур контракта. Полную проверку по JSON-схемам делает backend
 * CI; здесь — быстрый барьер против регрессий, которые уже встречались:
 * отсутствие `schema_version`/`source_seq` и не-UUID идентификаторы событий.
 */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

describe('фикстуры контракта', () => {
  it('события: schema_version=1, source_seq и UUID-идентификаторы (event-v1)', () => {
    for (const e of events) {
      expect(e.schema_version).toBe(1);
      expect(typeof e.source_seq).toBe('number');
      for (const field of ['event_id', 'agent_id', 'deployment_id', 'instance_id'] as const) {
        expect(e[field], `${field}=${e[field]}`).toMatch(UUID);
      }
      // session_id может быть null (anyOf uuid|null).
      if (e.session_id !== null) expect(e.session_id).toMatch(UUID);
    }
  });

  it('сессия: форма AuthRead (вложенный user и csrf_token)', () => {
    expect(me).toMatchObject({ user: { id: expect.any(String), username: expect.any(String), role: expect.any(String) }, csrf_token: expect.any(String) });
  });

  it('профили: публичная конфигурация без паролей (ProfileConfigPublic)', () => {
    for (const p of profiles) {
      expect(p).toMatchObject({ id: expect.any(String), name: expect.any(String), revision: expect.any(Number) });
      expect(Array.isArray(p.config.credential_usernames)).toBe(true);
      expect(p.config).not.toHaveProperty('credentials');
    }
  });

  it('хосты: статусы из контракта (online/offline/revoked)', () => {
    for (const h of hosts) {
      expect(['online', 'offline', 'revoked']).toContain(h.status);
      expect(h).toHaveProperty('capabilities');
    }
  });
});
