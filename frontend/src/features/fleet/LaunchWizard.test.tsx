// @vitest-environment happy-dom
import { render, screen } from '@testing-library/react';
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

function open(path = '/fleet/new') {
  window.history.pushState({}, '', path);
  render(<App client={makeQueryClient()} />);
}
const JOB_WAIT = { timeout: 8000 };
const next = () => screen.getByRole('button', { name: 'Далее' });
/** Абзац по всему его тексту: «Ловушка <b>id</b> запущена» разбит на элементы. */
const paragraph = (re: RegExp) => (_: string, el: Element | null) => el?.tagName === 'P' && re.test(el.textContent ?? '');

describe('мастер запуска (F7)', () => {
  beforeEach(() => resetMockSession(true));

  it('хост → профиль → проверка → ловушка запущена', async () => {
    const user = userEvent.setup();
    let body: Record<string, unknown> | null = null;
    server.use(http.post('/api/v1/deployments', async ({ request }) => { body = (await request.clone().json()) as Record<string, unknown>; }));
    open();
    expect(await screen.findByRole('heading', { name: 'Запуск ловушки' })).toBeInTheDocument();
    expect(next()).toBeDisabled();
    await user.click(screen.getByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());

    await user.click(screen.getByRole('radio', { name: /Тестовый Linux-сервер/ }));
    expect(screen.getByLabelText('Ревизия')).toHaveValue('4');
    await user.click(next());

    expect(screen.getByText('Тестовый Linux-сервер · ревизия r4')).toBeInTheDocument();
    expect(screen.getByText('ssh/2222')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Запустить' }));

    expect(await screen.findByText(paragraph(/^Ловушка decoy-medium-\d+ запущена/), {}, JOB_WAIT)).toBeInTheDocument();
    expect(body).toMatchObject({ host_id: 'host-runtime-01', profile_id: 'p-linux-srv', profile_revision: 4 });
    expect(typeof body!.idempotency_key).toBe('string');
    expect(body).not.toHaveProperty('level');
  }, 15_000);

  it('хост без связи выбрать нельзя; у хоста с ошибками — причина', async () => {
    open();
    const offline = await screen.findByRole('radio', { name: /host-runtime-03/ });
    expect(offline).toBeDisabled();
    expect(screen.getByText('работает с ошибками: очередь 340, мало места на диске')).toBeInTheDocument();
  });

  it('High — только на хост с KVM', async () => {
    const user = userEvent.setup();
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    expect(screen.getByRole('radio', { name: /Настоящая Linux VM/ })).toBeDisabled();
    expect(screen.getByText('High нужна виртуализация KVM — на этом хосте её нет')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Назад' }));
    await user.click(screen.getByRole('radio', { name: /host-runtime-02/ }));
    await user.click(next());
    expect(screen.getByRole('radio', { name: /Настоящая Linux VM/ })).toBeEnabled();
  });

  it('«Назад» не теряет выбор; можно выбрать старую ревизию', async () => {
    const user = userEvent.setup();
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    await user.click(screen.getByRole('radio', { name: /Веб-панель/ }));
    await user.selectOptions(screen.getByLabelText('Ревизия'), '1');
    await user.click(screen.getByRole('button', { name: 'Назад' }));
    expect(screen.getByRole('radio', { name: /host-runtime-01/ })).toBeChecked();
    await user.click(next());
    expect(screen.getByRole('radio', { name: /Веб-панель/ })).toBeChecked();
    expect(screen.getByLabelText('Ревизия')).toHaveValue('1');
    await user.click(next());
    expect(screen.getByText(/ревизия r1/)).toBeInTheDocument();
  });

  it('запуск не удался — итог без «запущена» и ссылка в парк', async () => {
    const user = userEvent.setup();
    failNextJobs('образ не найден');
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    await user.click(screen.getByRole('radio', { name: /Веб-панель/ }));
    await user.click(next());
    await user.click(screen.getByRole('button', { name: 'Запустить' }));
    expect(await screen.findByText('ошибка: образ не найден', {}, JOB_WAIT)).toBeInTheDocument();
    expect(screen.queryByText(paragraph(/запущена/))).toBeNull();
    expect(screen.getByRole('link', { name: 'Перейти в парк' })).toBeInTheDocument();
  }, 15_000);

  it('ошибка API при отправке — остаёмся на проверке с сообщением', async () => {
    const user = userEvent.setup();
    server.use(http.post('/api/v1/deployments', () => HttpResponse.json({ detail: 'Хост больше не на связи' }, { status: 409 })));
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    await user.click(screen.getByRole('radio', { name: /Веб-панель/ }));
    await user.click(next());
    await user.click(screen.getByRole('button', { name: 'Запустить' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Хост больше не на связи');
    expect(screen.getByRole('button', { name: 'Запустить' })).toBeEnabled();
  });

  it('наблюдатель по прямой ссылке видит объяснение', async () => {
    server.use(http.get('/api/v1/auth/me', () => HttpResponse.json({ user_id: 'u-2', login: 'viewer', role: 'viewer', csrf_token: 't' })));
    open();
    expect(await screen.findByText('Запускать ловушки могут только оператор и администратор.')).toBeInTheDocument();
    expect(screen.queryByRole('radio')).toBeNull();
  });
});
