// @vitest-environment happy-dom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const consoleOf = () => screen.getByRole('region', { name: 'Юми › анализ' });
const row = async (id: string) => (await screen.findByText(id)).closest('tr')!;

describe('Юми в приложении (раздел 9)', () => {
  beforeEach(() => resetMockSession(true));

  it('после входа — одно приветствие; без WebGL окно Юми показывает портрет и подпись', async () => {
    window.history.pushState({}, '', '/fleet');
    render(<App client={makeQueryClient()} />);
    await row('decoy-low-01');
    const log = consoleOf();
    expect(await within(log).findByText('Я на посту. Слежу за ловушками.', { selector: '[aria-live]' })).toBeInTheDocument();
    // Видимых строк ровно одна: StrictMode не задвоил приветствие.
    expect(log.querySelectorAll('p[aria-hidden="true"]')).toHaveLength(1);
    const slot = screen.getByRole('region', { name: 'Юми' });
    expect(within(slot).getByText('3D недоступно — реплики в консоли.')).toBeInTheDocument();
    expect(slot.querySelector('canvas')).toBeNull();
  });

  it('команда: «отправила» после приёма центром, затем итог задания', async () => {
    const user = userEvent.setup();
    window.history.pushState({}, '', '/fleet');
    render(<App client={makeQueryClient()} />);
    const low = await row('decoy-low-01');
    await user.click(within(low).getByRole('button', { name: 'Остановить' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Подтвердить' }));
    await within(consoleOf()).findByText('Отправила команду «Остановить». Жду агента.', { selector: '[aria-live]' });
    await within(consoleOf()).findByText('Команда «Остановить» выполнена.', { selector: '[aria-live]' }, { timeout: 8000 });
    // Узел живой области пересоздаётся на каждую реплику — ищем заново.
    expect(consoleOf().querySelector('[aria-live]')!.textContent).toBe('Команда «Остановить» выполнена.');
  }, 15_000);
});
