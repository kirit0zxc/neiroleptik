// @vitest-environment happy-dom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import { failNextJobs } from '../../mocks/jobs';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function open() {
  window.history.pushState({}, '', '/fleet');
  render(<App client={makeQueryClient()} />);
}
const row = async (id: string) => (await screen.findByText(id)).closest('tr')!;
// Задание продвигается на шаг за опрос (раз в секунду): до итога ~4 с.
const JOB_WAIT = { timeout: 8000 };

describe('команды ловушек (F6)', () => {
  beforeEach(() => resetMockSession(true));

  it('применить профиль: ход задания до «выполнено», drift исчезает', async () => {
    const user = userEvent.setup();
    open();
    const medium = await row('decoy-medium-01');
    expect(within(medium).getByText('drift: r3 → r4')).toBeInTheDocument();
    await user.click(within(medium).getByRole('button', { name: 'Применить профиль' }));

    const status = await within(medium).findByRole('status');
    expect(status).toHaveTextContent(/Применить профиль: (поставлено в очередь|взято агентом|выполняется)/);
    // Пока задание идёт, кнопки неактивны.
    expect(within(medium).getByRole('button', { name: 'Остановить' })).toBeDisabled();

    await within(medium).findByText('Применить профиль: выполнено', {}, JOB_WAIT);
    expect(await within(await row('decoy-medium-01')).findByText('работает')).toBeInTheDocument();
  }, 15_000);

  it('reset High: подтверждение; «Отмена» ничего не отправляет', async () => {
    const user = userEvent.setup();
    let sent = 0;
    // Счётчик без ответа: запрос проходит дальше, в обычный мок.
    server.use(http.post('/api/v1/deployments/:id/commands', () => { sent += 1; }));
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Сбросить VM' }));
    const dialog = await screen.findByRole('dialog', { name: 'Сбросить VM — decoy-high-01?' });
    expect(dialog).toHaveTextContent('доказательства сохранятся');
    await user.click(within(dialog).getByRole('button', { name: 'Отмена' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(sent).toBe(0);

    await user.click(within(high).getByRole('button', { name: 'Сбросить VM' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Подтвердить' }));
    await within(high).findByRole('status');
    expect(sent).toBe(1);
  });

  it('каждое нажатие — новый idempotency_key, команда уходит с CSRF', async () => {
    const user = userEvent.setup();
    const bodies: Array<{ command: string; idempotency_key: string; csrf: string | null }> = [];
    server.use(http.post('/api/v1/deployments/:id/commands', async ({ request }) => {
      const b = (await request.json()) as { command: string; idempotency_key: string };
      bodies.push({ ...b, csrf: request.headers.get('X-CSRF-Token') });
      return HttpResponse.json({ detail: 'стоп' }, { status: 409 });
    }));
    open();
    const low = await row('decoy-low-01');
    const stop = within(low).getByRole('button', { name: 'Остановить' });
    await user.click(stop);
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Подтвердить' }));
    await within(low).findByRole('alert');
    await user.click(stop);
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Подтвердить' }));
    await within(low).findByRole('alert');
    expect(bodies).toHaveLength(2);
    expect(bodies[0]!.idempotency_key).not.toBe(bodies[1]!.idempotency_key);
    expect(bodies.every((b) => b.command === 'stop' && b.csrf === 'mock-csrf-token')).toBe(true);
  });

  it('409 — у ловушки уже есть задание: понятное сообщение', async () => {
    const user = userEvent.setup();
    server.use(http.post('/api/v1/deployments/:id/commands', () =>
      HttpResponse.json({ detail: 'У ловушки уже выполняется задание' }, { status: 409 })));
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    expect(await within(high).findByRole('alert')).toHaveTextContent('У ловушки уже выполняется задание');
  });

  it('задание завершилось ошибкой — текст ошибки, кнопки снова доступны', async () => {
    const user = userEvent.setup();
    failNextJobs('KVM недоступен на хосте');
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    await within(high).findByText('Запустить: ошибка: KVM недоступен на хосте', {}, JOB_WAIT);
    expect(within(high).getByRole('button', { name: 'Запустить' })).toBeEnabled();
  }, 15_000);

  it('наблюдатель не видит команд (F2)', async () => {
    server.use(http.get('/api/v1/auth/me', () => HttpResponse.json({ user_id: 'u-2', login: 'viewer', role: 'viewer', csrf_token: 't' })));
    open();
    await row('decoy-medium-01');
    expect(screen.queryByRole('columnheader', { name: 'Действия' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Остановить' })).toBeNull();
  });
});
