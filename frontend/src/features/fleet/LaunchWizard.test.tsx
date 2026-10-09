// @vitest-environment happy-dom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function open(path = '/fleet/new') {
  window.history.pushState({}, '', path);
  render(<App client={makeQueryClient()} />);
}
const next = () => screen.getByRole('button', { name: 'Далее' });
/** Абзац по всему его тексту: «Ловушка <b>id</b> запланирована» разбит на элементы. */
const paragraph = (re: RegExp) => (_: string, el: Element | null) => el?.tagName === 'P' && re.test(el.textContent ?? '');

describe('мастер запуска (F7)', () => {
  beforeEach(() => resetMockSession(true));

  it('хост → профиль → проверка → ловушка запланирована (контракт)', async () => {
    const user = userEvent.setup();
    let body: Record<string, unknown> | null = null;
    server.use(http.post('/api/v1/deployments', async ({ request }) => {
      body = (await request.clone().json()) as Record<string, unknown>;
      return HttpResponse.json({
        id: 'decoy-medium-9', host_id: 'host-runtime-01', profile_id: 'p-linux-srv',
        revision: 4, state: 'planned', created_at: new Date().toISOString(),
      }, { status: 201 });
    }));
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
    await user.click(screen.getByRole('button', { name: 'Запланировать' }));

    expect(await screen.findByText(paragraph(/^Ловушка decoy-medium-9 запланирована/))).toBeInTheDocument();
    // Тело запроса — строго по контракту DeploymentCreate.
    expect(body).toEqual({ host_id: 'host-runtime-01', profile_id: 'p-linux-srv', revision: 4 });
  });

  it('хост без связи выбрать нельзя', async () => {
    open();
    const offline = await screen.findByRole('radio', { name: /host-runtime-03/ });
    expect(offline).toBeDisabled();
    expect(screen.getByText('нет связи с агентом')).toBeInTheDocument();
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

  it('ошибка API при отправке — остаёмся на проверке с сообщением', async () => {
    const user = userEvent.setup();
    server.use(http.post('/api/v1/deployments', () => HttpResponse.json({ detail: 'Хост больше не на связи' }, { status: 409 })));
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    await user.click(screen.getByRole('radio', { name: /Веб-панель/ }));
    await user.click(next());
    await user.click(screen.getByRole('button', { name: 'Запланировать' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Хост больше не на связи');
    expect(screen.getByRole('button', { name: 'Запланировать' })).toBeEnabled();
  });

  it('наблюдатель по прямой ссылке видит объяснение', async () => {
    server.use(http.get('/api/v1/auth/me', () => HttpResponse.json({ user: { id: 'u-2', username: 'viewer', role: 'viewer' }, csrf_token: 't' })));
    open();
    expect(await screen.findByText('Запускать ловушки могут только оператор и администратор.')).toBeInTheDocument();
    expect(screen.queryByRole('radio')).toBeNull();
  });
});
