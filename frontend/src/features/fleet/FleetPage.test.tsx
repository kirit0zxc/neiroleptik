// @vitest-environment happy-dom
import { render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import type { Deployment } from '../../shared/api/types';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); vi.unstubAllEnvs(); });
afterAll(() => server.close());

function open() {
  window.history.pushState({}, '', '/fleet');
  render(<App client={makeQueryClient()} />);
}

describe('парк: настоящий DeploymentRead и демо-превью', () => {
  beforeEach(() => resetMockSession(true));

  it.each(['0', ''])('реальный режим %s показывает planned и revision, не создаёт команд', async (mode) => {
    vi.stubEnv('VITE_API_MOCK', mode);
    const deployment: Deployment = {
      id: 'cdb96a9a-8315-4040-9ef5-08a741af7c90',
      host_id: '66ad1c29-11af-4e34-b1ec-31efb5a74972',
      profile_id: 'b4782d54-82ca-4540-a886-c70cce5d235b',
      revision: 7, state: 'planned', created_at: '2026-10-09T12:00:00Z',
    };
    let unsupportedCalls = 0;
    server.use(
      http.get('/api/v1/deployments', () => HttpResponse.json([deployment])),
      http.post('/api/v1/deployments/:id/commands', () => { unsupportedCalls++; return HttpResponse.json({}, { status: 404 }); }),
      http.get('/api/v1/jobs/:id', () => { unsupportedCalls++; return HttpResponse.json({}, { status: 404 }); }),
    );
    open();
    const row = (await screen.findByText(deployment.id)).closest('tr')!;
    expect(within(row).getByText(`${deployment.profile_id} · r7`)).toBeInTheDocument();
    expect(within(row).getByText('запланирована')).toBeInTheDocument();
    expect(within(row).getByText('—')).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Действия' })).toBeNull();
    expect(within(row).queryByRole('button')).toBeNull();
    expect(unsupportedCalls).toBe(0);
  });

  it('явный mock-режим сохраняет drift, уровень и команды', async () => {
    vi.stubEnv('VITE_API_MOCK', '1');
    open();
    const row = (await screen.findByText('decoy-medium-01')).closest('tr')!;
    expect(within(row).getByText('drift: r3 → r4')).toBeInTheDocument();
    expect(within(row).getByText('Medium')).toBeInTheDocument();
    expect(within(row).getByText(/· r4/)).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Применить профиль' })).toBeEnabled();
  });
});
