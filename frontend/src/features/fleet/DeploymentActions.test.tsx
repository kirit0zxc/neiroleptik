// @vitest-environment happy-dom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import { expireNextJobs, failNextJobs } from '../../mocks/jobs';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function open() {
  window.history.pushState({}, '', '/fleet');
  render(<App client={makeQueryClient()} />);
}
const row = async (id: string) => (await screen.findByText(id)).closest('tr')!;
const confirmDialog = async () => within(await screen.findByRole('dialog')).getByRole('button', { name: 'Подтвердить' });
// Задание продвигается на шаг за опрос (раз в секунду): до итога ~3–4 с.
const JOB_WAIT = { timeout: 8000 };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Sent = { command: string; idempotency_key: string; expected_generation: number; csrf: string | null };
/** Записывает тела отправленных команд; без ответа — запрос идёт дальше, в мок по контракту. */
const recordCommands = (into: Sent[]) => http.post('/api/v1/deployments/:id/commands', async ({ request }) => {
  const b = (await request.clone().json()) as Omit<Sent, 'csrf'>;
  into.push({ ...b, csrf: request.headers.get('X-CSRF-Token') });
  return undefined;
});
const job = (patch: Record<string, unknown>) => ({
  job_id: 'job-1', deployment_id: 'decoy-high-01', command: 'start', state: 'requested', error: null,
  error_code: null, generation: 3, created_at: '2026-10-10T00:00:00Z', completed_at: null, ...patch,
});

// Команды идут на API центра (в тестах — MSW по контракту specs/commands.md).
describe('команды ловушек на API (F6)', () => {
  beforeEach(() => resetMockSession(true));

  it('остановка Low: тело по контракту, ход «ожидает → выполняется → успешно», затем «остановлена»', async () => {
    const user = userEvent.setup();
    const sent: Sent[] = [];
    server.use(recordCommands(sent));
    open();
    const low = await row('decoy-low-01');
    expect(within(low).queryByRole('button', { name: 'Применить профиль' })).toBeNull();
    expect(within(low).queryByRole('button', { name: 'Сменить учётки' })).toBeNull();
    await user.click(within(low).getByRole('button', { name: 'Остановить' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('перестанет принимать подключения');
    await user.click(await confirmDialog());

    expect(await within(low).findByText(/Остановить: (ожидает|выполняется)/)).toBeInTheDocument();
    // Защита от повторных кликов; фокус после закрытия диалога — на ходе задания.
    for (const b of within(low).queryAllByRole('button')) expect(b).toBeDisabled();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(within(low).getByRole('status')).toHaveFocus();
    await within(low).findByText('Остановить: успешно', {}, JOB_WAIT);
    expect(await within(await row('decoy-low-01')).findByText('остановлена')).toBeInTheDocument();

    expect(sent).toHaveLength(1);
    expect(sent[0]).toEqual({ command: 'stop', idempotency_key: expect.stringMatching(UUID), expected_generation: 1, csrf: 'mock-csrf-token' });

    // Запуск — новое действие: новый ключ и уже новое поколение ловушки.
    await user.click(within(await row('decoy-low-01')).getByRole('button', { name: 'Запустить' }));
    await within(await row('decoy-low-01')).findByText('Запустить: успешно', {}, JOB_WAIT);
    expect(await within(await row('decoy-low-01')).findByText('работает')).toBeInTheDocument();
    expect(sent).toHaveLength(2);
    expect(sent[1]).toMatchObject({ command: 'start', expected_generation: 2 });
    expect(sent[1]!.idempotency_key).not.toBe(sent[0]!.idempotency_key);
  }, 20_000);

  it('запрос дошёл, ответ потерян: «Повторить» с тем же UUID получает то же задание', async () => {
    const user = userEvent.setup();
    const sent: Sent[] = [];
    // Сторона центра: задание создаётся по ключу один раз.
    const created = new Map<string, string>();
    let lose = true;
    server.use(
      http.post('/api/v1/deployments/:id/commands', async ({ request }) => {
        const b = (await request.json()) as Omit<Sent, 'csrf'>;
        sent.push({ ...b, csrf: request.headers.get('X-CSRF-Token') });
        if (!created.has(b.idempotency_key)) created.set(b.idempotency_key, `job-${created.size + 1}`);
        if (lose) { lose = false; return HttpResponse.error(); }
        return HttpResponse.json({ job_id: created.get(b.idempotency_key) }, { status: 202 });
      }),
      http.get('/api/v1/jobs/:id', ({ params }) => HttpResponse.json(job({ job_id: params.id, state: 'succeeded' }))),
    );
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    const alert = await within(high).findByRole('alert');
    expect(alert).toHaveTextContent('Нет связи с сервером');
    // Пока команда не отправлена, другие команды ловушки неактивны.
    expect(within(high).getByRole('button', { name: 'Сбросить VM' })).toBeDisabled();
    await user.click(within(alert).getByRole('button', { name: 'Повторить' }));
    await within(high).findByText('Запустить: успешно');
    expect(sent).toHaveLength(2);
    expect(sent[1]).toEqual(sent[0]);
    expect(created.size).toBe(1);
  });

  it('постоянный сбой отправки: «Отменить» снимает блокировку без перезагрузки', async () => {
    const user = userEvent.setup();
    server.use(http.post('/api/v1/deployments/:id/commands', () => HttpResponse.json({ detail: 'Ошибка сервера' }, { status: 500 })));
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    const alert = await within(high).findByRole('alert');
    expect(within(high).getByRole('button', { name: 'Сбросить VM' })).toBeDisabled();
    await user.click(within(alert).getByRole('button', { name: 'Отменить' }));
    expect(within(high).queryByRole('alert')).toBeNull();
    expect(within(high).getByRole('button', { name: 'Сбросить VM' })).toBeEnabled();
  });

  it('STALE_GENERATION: данные перечитаны, «Повторить» — новое действие со свежим поколением', async () => {
    const user = userEvent.setup();
    const sent: Sent[] = [];
    let first = true;
    let generation = 2;
    server.use(
      http.post('/api/v1/deployments/:id/commands', async ({ request }) => {
        sent.push({ ...((await request.json()) as Omit<Sent, 'csrf'>), csrf: null });
        if (first) {
          first = false;
          generation = 5; // кто-то успел изменить ловушку
          return HttpResponse.json({ detail: 'Перечитайте поколение ловушки', code: 'STALE_GENERATION' }, { status: 409 });
        }
        return HttpResponse.json({ job_id: 'job-1' }, { status: 202 });
      }),
      // Парк с одной ловушкой, чьё поколение меняется после отказа.
      http.get('/api/v1/deployments', () => HttpResponse.json([{
        id: 'decoy-high-01', host_id: 'host-runtime-02', profile_id: 'p-linux-vm', revision: 1, state: 'failed',
        interaction_level: 'high', desired_revision: 1, applied_revision: 1, generation, instance_id: null,
        created_at: '2026-10-09T09:10:00Z',
      }])),
      http.get('/api/v1/jobs/:id', () => HttpResponse.json(job({}))),
    );
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    const alert = await within(high).findByRole('alert');
    expect(alert).toHaveTextContent('Ловушку уже изменили — данные обновлены.');
    await user.click(await within(alert).findByRole('button', { name: 'Повторить' }));
    expect(await within(high).findByText('Запустить: ожидает')).toBeInTheDocument();
    expect(sent.map((b) => b.expected_generation)).toEqual([2, 5]);
    expect(sent[1]!.idempotency_key).not.toBe(sent[0]!.idempotency_key);
  });

  it('STALE_GENERATION, а команда после перечитывания недоступна — без обещания «Повторить»', async () => {
    const user = userEvent.setup();
    let state = 'failed';
    server.use(
      http.post('/api/v1/deployments/:id/commands', () => {
        state = 'deploying'; // чужая команда уже запускает ловушку
        return HttpResponse.json({ detail: 'Перечитайте поколение ловушки', code: 'STALE_GENERATION' }, { status: 409 });
      }),
      http.get('/api/v1/deployments', () => HttpResponse.json([{
        id: 'decoy-high-01', host_id: 'host-runtime-02', profile_id: 'p-linux-vm', revision: 1, state,
        interaction_level: 'high', desired_revision: 1, applied_revision: 1, generation: state === 'failed' ? 2 : 3,
        instance_id: null, created_at: '2026-10-09T09:10:00Z',
      }])),
    );
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    const alert = await within(high).findByRole('alert');
    expect(await within(alert).findByText(/Команда больше недоступна в текущем состоянии/)).toBeInTheDocument();
    expect(within(alert).queryByRole('button')).toBeNull();
    expect(alert).not.toHaveTextContent('Повторить?');
  });

  it('отказ центра с кодом — понятная причина без повторной отправки', async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(http.post('/api/v1/deployments/:id/commands', () => {
      calls += 1;
      return HttpResponse.json({ detail: 'У ловушки уже есть незавершённая команда', code: 'COMMAND_BUSY' }, { status: 409 });
    }));
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    const alert = await within(high).findByRole('alert');
    expect(alert).toHaveTextContent('У ловушки уже выполняется команда');
    expect(within(alert).queryByRole('button', { name: 'Повторить' })).toBeNull();
    expect(calls).toBe(1);
  });

  it('задание завершилось ошибкой драйвера — понятная причина, кнопки снова доступны', async () => {
    const user = userEvent.setup();
    failNextJobs('BACKEND_UNAVAILABLE', 'KVM недоступен на хосте');
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    await within(high).findByText('Запустить: ошибка: среда запуска недоступна (Docker или KVM)', {}, JOB_WAIT);
    expect(await within(await row('decoy-high-01')).findByRole('button', { name: 'Запустить' })).toBeEnabled();
  }, 15_000);

  it('агент не взял команду (DELIVERY_EXPIRED) — «истёк срок», состояние ловушки прежнее', async () => {
    const user = userEvent.setup();
    expireNextJobs('delivery');
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    await within(high).findByText('Запустить: истёк срок — агент не выполнил команду вовремя', {}, JOB_WAIT);
    const after = await row('decoy-high-01');
    expect(within(after).getByText('ошибка')).toBeInTheDocument();
    expect(within(after).getByRole('button', { name: 'Запустить' })).toBeEnabled();
  }, 15_000);

  it('истёк срок выполнения (EXECUTION_EXPIRED) — ловушка «состояние не подтверждено», можно только остановить', async () => {
    const user = userEvent.setup();
    expireNextJobs('execution');
    open();
    const low = await row('decoy-low-01');
    await user.click(within(low).getByRole('button', { name: 'Остановить' }));
    await user.click(await confirmDialog());
    await within(low).findByText('Остановить: истёк срок — агент не выполнил команду вовремя', {}, JOB_WAIT);
    const after = await row('decoy-low-01');
    expect(await within(after).findByText('состояние не подтверждено')).toBeInTheDocument();
    expect(within(after).getAllByRole('button').map((b) => b.textContent)).toEqual(['Остановить']);
  }, 15_000);

  it('сбой сети при опросе задания — «нет связи», не успех; опрос продолжается до итога', async () => {
    const user = userEvent.setup();
    let drops = 2;
    server.use(http.get('/api/v1/jobs/:id', () => {
      if (drops > 0) { drops -= 1; return HttpResponse.error(); }
      return undefined;
    }));
    open();
    const low = await row('decoy-low-01');
    await user.click(within(low).getByRole('button', { name: 'Остановить' }));
    await user.click(await confirmDialog());
    expect(await within(low).findByText('нет связи — проверяем снова')).toBeInTheDocument();
    expect(within(low).queryByText(/успешно/)).toBeNull();
    for (const b of within(low).queryAllByRole('button')) expect(b).toBeDisabled();
    await within(low).findByText('Остановить: успешно', {}, { timeout: 12_000 });
  }, 20_000);

  it('отказ сервера при опросе (404) — понятная причина, опрос остановлен, кнопки доступны', async () => {
    const user = userEvent.setup();
    let polls = 0;
    server.use(http.get('/api/v1/jobs/:id', () => {
      polls += 1;
      return HttpResponse.json({ detail: 'Задание не найдено' }, { status: 404 });
    }));
    open();
    const high = await row('decoy-high-01');
    await user.click(within(high).getByRole('button', { name: 'Запустить' }));
    expect(await within(high).findByText('не удалось проверить задание: Задание не найдено')).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 1500));
    expect(polls).toBe(1);
    expect(within(await row('decoy-high-01')).getByRole('button', { name: 'Запустить' })).toBeEnabled();
  }, 10_000);

  it('reset High: подтверждение; «Отмена» ничего не отправляет и возвращает фокус', async () => {
    const user = userEvent.setup();
    const sent: Sent[] = [];
    server.use(recordCommands(sent));
    open();
    const high = await row('decoy-high-01');
    const reset = within(high).getByRole('button', { name: 'Сбросить VM' });
    await user.click(reset);
    const dialog = await screen.findByRole('dialog', { name: 'Сбросить VM — decoy-high-01?' });
    expect(dialog).toHaveTextContent('доказательства сохранятся');
    await user.click(within(dialog).getByRole('button', { name: 'Отмена' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(reset).toHaveFocus();
    await new Promise((r) => setTimeout(r, 100));
    expect(sent).toHaveLength(0);

    await user.click(reset);
    await user.click(await confirmDialog());
    await within(high).findByText(/Сбросить VM: /);
    expect(sent).toEqual([expect.objectContaining({ command: 'reset', expected_generation: 2 })]);
  });

  it('права: оператор видит команды, наблюдатель — нет (F2)', async () => {
    open();
    expect(within(await row('decoy-low-01')).getByRole('button', { name: 'Остановить' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Действия' })).toBeInTheDocument();
  });

  it('наблюдатель не видит команд (F2)', async () => {
    server.use(http.get('/api/v1/auth/me', () => HttpResponse.json({ user: { id: 'u-2', username: 'viewer', role: 'viewer' }, csrf_token: 't' })));
    open();
    // Роль уже загружена: шапка показывает наблюдателя.
    expect(await screen.findByText(/viewer · наблюдатель/)).toBeInTheDocument();
    await row('decoy-medium-01');
    expect(screen.queryByRole('columnheader', { name: 'Действия' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Остановить' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Запустить' })).toBeNull();
  });
});
