// @vitest-environment happy-dom
import { render, screen, within } from '@testing-library/react';
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

function open(path: string) {
  window.history.pushState({}, '', path);
  render(<App client={makeQueryClient()} />);
}

describe('форма профиля (F8, F9)', () => {
  beforeEach(() => resetMockSession(true));

  it('создание: ошибки у полей, затем сохранение ревизии r1 и переход к профилю', async () => {
    const user = userEvent.setup();
    open('/profiles/new');
    await screen.findByRole('heading', { name: 'Новый профиль' });

    // Без имени не сохраняется — ошибка у поля.
    await user.click(screen.getByRole('button', { name: 'Создать профиль' }));
    expect(await screen.findByText('Введите название')).toBeInTheDocument();

    await user.type(screen.getByLabelText('Название'), 'Камера на складе');
    const port = within(screen.getByRole('group', { name: 'Сервис 1' })).getByLabelText('Порт');
    await user.clear(port);
    await user.type(port, '70000');
    await user.click(screen.getByRole('button', { name: 'Создать профиль' }));
    expect(await screen.findByText('Порт до 65535')).toBeInTheDocument();

    await user.clear(port);
    await user.type(port, '8081');
    await user.click(screen.getByRole('button', { name: 'Создать профиль' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Сохранено: ревизия r1');
    expect(window.location.pathname).toBe('/profiles/p-new-1');
    expect(await screen.findByText(/r1/, { selector: 'b' })).toBeInTheDocument();
  });

  it('повтор порта отмечен у второго сервиса', async () => {
    const user = userEvent.setup();
    open('/profiles/new');
    await user.type(await screen.findByLabelText('Название'), 'x');
    await user.click(screen.getByRole('button', { name: '+ Сервис' }));
    const second = screen.getByRole('group', { name: 'Сервис 2' });
    await user.type(within(second).getByLabelText('Порт'), '8080');
    await user.click(screen.getByRole('button', { name: 'Создать профиль' }));
    expect(await within(second).findByText('Порт уже занят сервисом №1')).toBeInTheDocument();
  });

  it('Medium показывает учётки-приманки; переход на Low их убирает', async () => {
    const user = userEvent.setup();
    open('/profiles/p-linux-srv');
    expect(await screen.findByRole('region', { name: 'Учётные данные-приманки' })).toBeInTheDocument();
    expect(screen.getByDisplayValue('admin')).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Уровень взаимодействия'), 'low');
    expect(screen.queryByRole('region', { name: 'Учётные данные-приманки' })).toBeNull();
    // SSH на Low недопустим — сервис заменён набором Low по умолчанию,
    // и выпадающий список показывает то, что реально в форме.
    const svc = screen.getByRole('group', { name: 'Сервис 1' });
    expect(within(svc).getByLabelText('Протокол')).toHaveValue('http');
    expect(within(svc).getByLabelText('Порт')).toHaveValue('8080');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Сохранено: ревизия r5');
  });

  it('сохранение существующего профиля создаёт следующую ревизию', async () => {
    const user = userEvent.setup();
    open('/profiles/p-linux-srv');
    const name = await screen.findByLabelText('Название');
    await user.clear(name);
    await user.type(name, 'Сервер БД');
    // Сервер не отдаёт пароли учёток — при сохранении их вводят заново.
    const creds = screen.getByRole('group', { name: 'Учётка 1' });
    await user.type(within(creds).getByLabelText('Пароль'), 'admin123');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Сохранено: ревизия r5');
    // p-linux-srv начинается с r1..r4; сохранение добавляет r5.
    const revisions = screen.getByRole('region', { name: 'Ревизии' });
    expect(within(revisions).getAllByRole('listitem')).toHaveLength(5);
  });

  it('наблюдатель видит профиль, но не может его менять (F2)', async () => {
    server.use(http.get('/api/v1/auth/me', () => HttpResponse.json({ user: { id: 'u-2', username: 'viewer', role: 'viewer' }, csrf_token: 't' })));
    open('/profiles/p-linux-srv');
    expect(await screen.findByText('Роль «наблюдатель» — только просмотр.')).toBeInTheDocument();
    expect(screen.getByLabelText('Название')).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Сохранить новую ревизию' })).toBeNull();
  });

  it('ошибка API при сохранении показана, данные формы не теряются', async () => {
    const user = userEvent.setup();
    server.use(http.patch('/api/v1/profiles/:id', () =>
      HttpResponse.json({ detail: 'Профиль уже изменён: перечитайте текущую версию' }, { status: 409 })));
    open('/profiles/p-linux-srv');
    const name = await screen.findByLabelText('Название');
    await user.type(name, ' 2');
    await user.type(within(screen.getByRole('group', { name: 'Учётка 1' })).getByLabelText('Пароль'), 'admin123');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Профиль уже изменён: перечитайте текущую версию');
    expect(name).toHaveValue('Тестовый Linux-сервер 2');
  });

  it('422 от сервера: ошибки показаны у своих полей', async () => {
    const user = userEvent.setup();
    server.use(http.patch('/api/v1/profiles/:id', () => HttpResponse.json({ detail: [
      { loc: ['body', 'config', 'services', 0, 'port'], type: 'value_error', msg: 'Порт занят другой ловушкой на хосте' },
      { loc: ['body', 'config', 'hostname'], type: 'value_error', msg: 'Имя хоста уже используется' },
    ] }, { status: 422 })));
    open('/profiles/p-linux-srv');
    await screen.findByLabelText('Название');
    await user.type(within(screen.getByRole('group', { name: 'Учётка 1' })).getByLabelText('Пароль'), 'admin123');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    const svc = screen.getByRole('group', { name: 'Сервис 1' });
    expect(await within(svc).findByText('Порт занят другой ловушкой на хосте')).toBeInTheDocument();
    expect(screen.getByText('Имя хоста уже используется')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Проверьте выделенные поля');
  });

  it('CSRF-токен из /auth/me уходит в X-CSRF-Token при сохранении', async () => {
    const user = userEvent.setup();
    let header: string | null = null;
    server.use(http.patch('/api/v1/profiles/:id', ({ request }) => {
      header = request.headers.get('X-CSRF-Token');
      return HttpResponse.json({ detail: 'стоп' }, { status: 409 });
    }));
    open('/profiles/p-linux-srv');
    await screen.findByLabelText('Название');
    await user.type(within(screen.getByRole('group', { name: 'Учётка 1' })).getByLabelText('Пароль'), 'admin123');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await screen.findByRole('alert');
    expect(header).toBe('mock-csrf-token');
  });

  it('список профилей ведёт в редактор', async () => {
    const user = userEvent.setup();
    open('/profiles');
    await user.click(await screen.findByRole('link', { name: 'Тестовый Linux-сервер' }));
    expect(await screen.findByLabelText('Название')).toHaveValue('Тестовый Linux-сервер');
  });
});
