// @vitest-environment happy-dom
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import openapi from '../../../contracts/openapi.json';
import { handlers, resetMockSession } from './handlers';

/**
 * Моки расследования не расходятся с общим контрактом: у каждой записи ровно
 * поля схемы из contracts/openapi.json, перечисления — из неё же.
 */
type Schema = { required?: string[]; properties?: Record<string, { enum?: unknown[]; const?: unknown }> };
const schemas = (openapi as unknown as { components: { schemas: Record<string, Schema> } }).components.schemas;

const server = setupServer(...handlers);
beforeAll(() => { server.listen({ onUnhandledRequest: 'error' }); resetMockSession(true, 'admin'); });
afterAll(() => server.close());

function conforms(item: Record<string, unknown>, name: string) {
  const s = schemas[name]!;
  // Ровно поля схемы: обязательные и все свойства (лишних нет, пропущенных нет).
  expect(Object.keys(item).sort(), name).toEqual([...(s.required ?? [])].sort());
  expect(Object.keys(item).sort(), name).toEqual(Object.keys(s.properties ?? {}).sort());
  for (const [key, prop] of Object.entries(s.properties ?? {})) {
    if (prop.enum) expect(prop.enum, `${name}.${key}`).toContain(item[key]);
    if ('const' in prop) expect(item[key], `${name}.${key}`).toBe(prop.const);
  }
}

describe('моки расследования по contracts/openapi.json', () => {
  it.each([
    ['/api/v1/alerts', 'AlertRead'],
    ['/api/v1/honeytokens', 'HoneytokenRead'],
    ['/api/v1/artifacts', 'ArtifactRead'],
  ])('%s → %s', async (path, name) => {
    const page = await (await fetch(`${location.origin}${path}`)).json() as { items: Record<string, unknown>[]; next_cursor: unknown };
    expect(Object.keys(page).sort()).toEqual(['items', 'next_cursor']);
    expect(page.items.length).toBeGreaterThan(0);
    for (const item of page.items) conforms(item, name);
  });

  it('срабатывания токена → HoneytokenTrigger', async () => {
    const tokens = await (await fetch(`${location.origin}/api/v1/honeytokens`)).json() as { items: { id: string }[] };
    const page = await (await fetch(`${location.origin}/api/v1/honeytokens/${tokens.items[0]!.id}/triggers`)).json() as { items: Record<string, unknown>[] };
    for (const item of page.items) conforms(item, 'HoneytokenTrigger');
  });

  it('lookup: дубли и пустой список — 422', async () => {
    const post = (refs: unknown) => fetch(`${location.origin}/api/v1/events/lookup`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'mock-csrf-token' }, body: JSON.stringify({ refs }),
    });
    const r = { agent_id: 'a', event_id: 'b' };
    expect((await post([])).status).toBe(422);
    expect((await post([r, r])).status).toBe(422);
    expect(Object.keys(await (await post([r])).json()).sort()).toEqual(['items', 'unavailable']);
  });
});
