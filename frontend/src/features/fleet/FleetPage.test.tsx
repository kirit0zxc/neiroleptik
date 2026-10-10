// @vitest-environment happy-dom
import { render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import type { Deployment } from '../../shared/api/types';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function open() {
  window.history.pushState({}, '', '/fleet');
  render(<App client={makeQueryClient()} />);
}

const live = (patch: Partial<Deployment>): Deployment => ({
  id: 'live-deployment', host_id: 'runtime', profile_id: 'profile', revision: 1,
  state: 'planned', created_at: '2026-10-10T00:00:00Z', interaction_level: 'low',
  desired_revision: 1, applied_revision: null, generation: 0, instance_id: null, ...patch,
});

describe('парк: DeploymentRead с центра', () => {
  beforeEach(() => resetMockSession(true));

  it.each([
    ['planned', 'запланирована', ['Запустить']],
    ['running', 'работает', ['Остановить']],
    ['stopped', 'остановлена', ['Запустить']],
    ['failed', 'ошибка', ['Запустить', 'Остановить']],
    ['unknown', 'состояние не подтверждено', ['Остановить']],
    ['deploying', 'разворачивается', []],
    ['stopping', 'останавливается', []],
  ] as const)('состояние %s: подпись и кнопки по specs/commands.md', async (state, label, buttons) => {
    server.use(http.get('/api/v1/deployments', () => HttpResponse.json([live({ state, applied_revision: 1 })])));
    open();
    const row = (await screen.findByText('live-deployment')).closest('tr')!;
    expect(within(row).getByText(label)).toBeInTheDocument();
    expect(within(row).getByText('Low')).toBeInTheDocument();
    expect(within(row).getByText('profile · r1')).toBeInTheDocument();
    expect(within(row).queryAllByRole('button').map((b) => b.textContent)).toEqual(buttons);
  });

  it('уровень и drift — из ответа центра', async () => {
    open();
    const row = (await screen.findByText('decoy-medium-01')).closest('tr')!;
    expect(within(row).getByText('drift: r3 → r4')).toBeInTheDocument();
    expect(within(row).getByText('Medium')).toBeInTheDocument();
    expect(within(row).getByText(/· r4/)).toBeInTheDocument();
  });
});
