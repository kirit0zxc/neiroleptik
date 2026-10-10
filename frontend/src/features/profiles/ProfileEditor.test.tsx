// @vitest-environment happy-dom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import profilesFixture from '../../../../contracts/fixtures/profiles.draft.json';
import type { Profile } from '../../shared/api/types';
import { handlers, resetMockSession } from '../../mocks/handlers';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function open(path: string) {
  const client = makeQueryClient();
  window.history.pushState({}, '', path);
  render(<App client={client} />);
  return client;
}

describe('форма профиля (F8, F9)', () => {
  beforeEach(() => resetMockSession(true));

  // Профиль, созданный в обход формы и нарушающий правила Low: открывается как
  // есть, ошибка видна сразу у поля, сохранение не уходит, пока её не исправят.
  const openBadLow = (services: Profile['config']['services']) => {
    const original = structuredClone(profilesFixture.find((p) => p.id === 'p-web-panel')) as Profile;
    original.config.services = services;
    const sent: unknown[] = [];
    server.use(
      http.get('/api/v1/profiles/p-web-panel', () => HttpResponse.json(original)),
      http.patch('/api/v1/profiles/p-web-panel', async ({ request }) => { sent.push(await request.json()); return HttpResponse.json(original); }),
    );
    open('/profiles/p-web-panel');
    return sent;
  };
  const DUPLICATE_HTTP = 'На Low не больше одного HTTP-сервиса — удалите лишний или выберите другой протокол';

  it('PR #9: Low-профиль с двумя HTTP открывается как есть, ошибка видна сразу', async () => {
    const user = userEvent.setup();
    const sent = openBadLow([{ protocol: 'http', port: 80, banner: 'a' }, { protocol: 'http', port: 81, banner: 'b' }]);
    const second = await screen.findByRole('group', { name: 'Сервис 2' });
    expect(within(screen.getByRole('group', { name: 'Сервис 1' })).getByLabelText('Порт')).toHaveValue('80');
    expect(within(second).getByLabelText('Порт')).toHaveValue('81');
    expect(within(second).getByLabelText('Протокол')).toHaveValue('http');
    expect(await within(second).findByText(DUPLICATE_HTTP)).toBeInTheDocument();

    // Переименование и «Сохранить» не отправляют ни исходную, ни подменённую
    // конфигурацию: сохранение останавливает проверка формы.
    await user.type(screen.getByLabelText('Название'), ' 2');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await within(second).findByText(DUPLICATE_HTTP)).toBeInTheDocument();
    expect(sent).toEqual([]);
    expect(within(second).getByLabelText('Порт')).toHaveValue('81');

    // Удалили лишний сервис — ошибка ушла, сохраняется ровно то, что осталось.
    await user.click(screen.getByRole('button', { name: 'Удалить сервис 2' }));
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect((sent[0] as { config: { services: unknown } }).config.services).toEqual([{ protocol: 'http', port: 80, banner: 'a' }]);
  });

  it.each([
    ['без баннера', '', 'У TCP баннер обязателен'],
    ['с кириллицей в баннере', 'Привет', 'Баннер TCP: только латиница, цифры, пробел и знаки ASCII'],
  ])('PR #9: TCP %s — значение сохраняется, ошибка у поля сразу, PATCH не уходит', async (_, banner, message) => {
    const user = userEvent.setup();
    const sent = openBadLow([{ protocol: 'http', port: 8080, banner: 'x' }, { protocol: 'tcp', port: 23, banner }]);
    const second = await screen.findByRole('group', { name: 'Сервис 2' });
    const field = within(second).getByLabelText('Баннер');
    expect(field).toHaveValue(banner);
    expect(await within(second).findByText(message)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    // Отправка формы асинхронна: дожидаемся её конца — фокус уходит на поле с
    // ошибкой — и только потом проверяем, что запрос не ушёл.
    await waitFor(() => expect(field).toHaveFocus());
    expect(sent).toEqual([]);
    expect(within(second).getByLabelText('Протокол')).toHaveValue('tcp');
  });

  it('PR #9: выбор повторного протокола показывает ошибку, а не сбрасывает список', async () => {
    const user = userEvent.setup();
    const sent = openBadLow([{ protocol: 'http', port: 8080, banner: 'x' }, { protocol: 'tcp', port: 23, banner: 'BusyBox' }]);
    const second = await screen.findByRole('group', { name: 'Сервис 2' });
    await user.selectOptions(within(second).getByLabelText('Протокол'), 'http');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await within(second).findByText(DUPLICATE_HTTP)).toBeInTheDocument();
    expect(sent).toEqual([]);
    expect(within(second).getByLabelText('Протокол')).toHaveValue('http');
    expect(within(second).getByLabelText('Порт')).toHaveValue('23');
    expect(within(screen.getByRole('group', { name: 'Сервис 1' })).getByLabelText('Порт')).toHaveValue('8080');
  });

  it('Low: у HTTP поле «Текст страницы-заглушки», «+ Сервис» добавляет TCP и пропадает', async () => {
    const user = userEvent.setup();
    open('/profiles/new');
    await screen.findByRole('heading', { name: 'Новый профиль' });
    const first = screen.getByRole('group', { name: 'Сервис 1' });
    expect(within(first).getByLabelText('Протокол')).toHaveValue('http');
    expect(within(first).getByLabelText('Текст страницы-заглушки')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '+ Сервис' }));
    const second = screen.getByRole('group', { name: 'Сервис 2' });
    expect(within(second).getByLabelText('Протокол')).toHaveValue('tcp');
    expect(within(second).getByLabelText('Баннер')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '+ Сервис' })).toBeNull();
    // Кнопка пропала — фокус не теряется, а правило объяснено.
    expect(within(second).getByLabelText('Протокол')).toHaveFocus();
    expect(screen.getByText('На Low — не больше одного TCP и одного HTTP.')).toBeInTheDocument();

    await user.type(screen.getByLabelText('Название'), 'Камера');
    await user.type(within(second).getByLabelText('Порт'), '23');
    await user.type(within(second).getByLabelText('Баннер'), 'Привет');
    await user.click(screen.getByRole('button', { name: 'Создать профиль' }));
    expect(await within(second).findByText('Баннер TCP: только латиница, цифры, пробел и знаки ASCII')).toBeInTheDocument();
  });

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
    expect(screen.getByText(/Сохранённые логины: admin/)).toBeInTheDocument();

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
    // Пароли остаются на сервере, переименование не требует их знания.
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
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await screen.findByRole('alert');
    expect(header).toBe('mock-csrf-token');
  });

  it('фоновая новая ревизия не меняет lock старой формы; 409 требует явного перечитывания', async () => {
    const user = userEvent.setup();
    const original = structuredClone(profilesFixture.find((p) => p.id === 'p-linux-srv')) as Profile;
    let latest = original;
    const sent: number[] = [];
    server.use(
      http.get('/api/v1/profiles/:id', () => HttpResponse.json(latest)),
      http.patch('/api/v1/profiles/:id', async ({ request }) => {
        const body = await request.json() as { expected_revision: number };
        sent.push(body.expected_revision);
        return HttpResponse.json({ detail: 'Конфликт версии' }, { status: 409 });
      }),
    );
    const client = open('/profiles/p-linux-srv');
    const name = await screen.findByLabelText('Название');
    await user.clear(name);
    await user.type(name, 'Моя правка');
    const banner = within(screen.getByRole('group', { name: 'Сервис 1' })).getByLabelText('Баннер');
    const oldBanner = (banner as HTMLInputElement).value;
    latest = { ...original, revision: original.revision + 1, name: 'Чужая правка',
      config: { ...original.config, services: original.config.services.map((svc) => ({ ...svc, banner: 'Новый чужой баннер' })) } };
    await act(async () => { await client.invalidateQueries({ queryKey: ['profiles', original.id], exact: true }); });
    expect(client.getQueryData<Profile>(['profiles', original.id])?.revision).toBe(latest.revision);
    expect(name).toHaveValue('Моя правка');
    expect(banner).toHaveValue(oldBanner);
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Конфликт версии');
    expect(sent).toEqual([original.revision]);
    expect(name).toHaveValue('Моя правка');
    await user.click(screen.getByRole('button', { name: 'Перечитать профиль и сбросить мои изменения' }));
    await waitFor(() => expect(name).toHaveValue('Чужая правка'));
    expect(within(screen.getByRole('group', { name: 'Сервис 1' })).getByLabelText('Баннер')).toHaveValue('Новый чужой баннер');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await waitFor(() => expect(sent).toEqual([original.revision, latest.revision]));
  });

  it('rename сохраняет credentials:null и точный лимит байт', async () => {
    const user = userEvent.setup();
    const original = structuredClone(profilesFixture.find((p) => p.id === 'p-linux-srv')) as Profile;
    original.config.logging = { max_payload_bytes: 1536 };
    let body: { config: { credentials: unknown; logging: { max_payload_bytes: number } } } | undefined;
    server.use(
      http.get('/api/v1/profiles/:id', () => HttpResponse.json(original)),
      http.patch('/api/v1/profiles/:id', async ({ request }) => {
        body = await request.json() as typeof body;
        return HttpResponse.json({ ...original, revision: original.revision + 1 });
      }),
    );
    open('/profiles/p-linux-srv');
    await user.type(await screen.findByLabelText('Название'), ' 2');
    expect(screen.queryByLabelText('Пароль')).toBeNull();
    expect(screen.getByLabelText('Лимит payload, байт')).toHaveValue('1536');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await screen.findByRole('status');
    expect(body?.config.credentials).toBeNull();
    expect(body?.config.logging.max_payload_bytes).toBe(1536);
  });

  it('явная замена учёток валидируется; возврат к сохранению не отправляет секреты', async () => {
    const user = userEvent.setup();
    let credentials: unknown = 'not-sent';
    server.use(http.patch('/api/v1/profiles/:id', async ({ request }) => {
      credentials = (await request.json() as { config: { credentials: unknown } }).config.credentials;
      return HttpResponse.json({ detail: 'стоп' }, { status: 409 });
    }));
    open('/profiles/p-linux-srv');
    await user.click(await screen.findByLabelText('Заменить учётные данные'));
    expect(screen.getByLabelText('Логин')).toHaveValue('admin');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await screen.findByText('Введите пароль')).toBeInTheDocument();
    expect(credentials).toBe('not-sent');
    await user.type(screen.getByLabelText('Пароль'), 'replacement');
    await user.click(screen.getByLabelText('Заменить учётные данные'));
    expect(screen.queryByLabelText('Пароль')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await screen.findByRole('alert');
    expect(credentials).toBeNull();
  });

  it('новый Medium требует учётки и предлагает только SSH', async () => {
    const user = userEvent.setup();
    open('/profiles/new');
    await user.type(await screen.findByLabelText('Название'), 'SSH');
    await user.selectOptions(screen.getByLabelText('Уровень взаимодействия'), 'medium');
    expect(within(screen.getByLabelText('Протокол')).getAllByRole('option').map((option) => option.textContent)).toEqual(['SSH']);
    await user.click(screen.getByRole('button', { name: 'Создать профиль' }));
    expect(await screen.findByText('Medium и High требуют хотя бы одну учётку-приманку')).toBeInTheDocument();
  });

  it('после успешной записи следующая правка использует новую версию снимка', async () => {
    const user = userEvent.setup();
    let latest = structuredClone(profilesFixture.find((p) => p.id === 'p-linux-srv')) as Profile;
    const sent: number[] = [];
    server.use(
      http.get('/api/v1/profiles/:id', () => HttpResponse.json(latest)),
      http.patch('/api/v1/profiles/:id', async ({ request }) => {
        const body = await request.json() as { expected_revision: number; name: string };
        sent.push(body.expected_revision);
        latest = { ...latest, name: body.name, revision: latest.revision + 1 };
        return HttpResponse.json(latest);
      }),
    );
    open('/profiles/p-linux-srv');
    await user.type(await screen.findByLabelText('Название'), ' 1');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Сохранено: ревизия r5');
    await user.type(screen.getByLabelText('Название'), ' 2');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Сохранено: ревизия r6'));
    expect(sent).toEqual([4, 5]);
  });

  it('переход Medium → Low явно очищает серверные учётки', async () => {
    const user = userEvent.setup();
    let credentials: unknown = 'not-sent';
    server.use(http.patch('/api/v1/profiles/:id', async ({ request }) => {
      credentials = (await request.json() as { config: { credentials: unknown } }).config.credentials;
      return HttpResponse.json({ detail: 'стоп' }, { status: 409 });
    }));
    open('/profiles/p-linux-srv');
    await user.selectOptions(await screen.findByLabelText('Уровень взаимодействия'), 'low');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await screen.findByRole('alert');
    expect(credentials).toEqual([]);
    await user.selectOptions(screen.getByLabelText('Уровень взаимодействия'), 'medium');
    expect(screen.queryByLabelText('Заменить учётные данные')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    expect(await screen.findByText('Medium и High требуют хотя бы одну учётку-приманку')).toBeInTheDocument();
  });

  it('выбранная замена отправляет только введённые логины и пароли', async () => {
    const user = userEvent.setup();
    let credentials: unknown = 'not-sent';
    server.use(http.patch('/api/v1/profiles/:id', async ({ request }) => {
      credentials = (await request.json() as { config: { credentials: unknown } }).config.credentials;
      return HttpResponse.json({ detail: 'стоп' }, { status: 409 });
    }));
    open('/profiles/p-linux-srv');
    await user.click(await screen.findByLabelText('Заменить учётные данные'));
    await user.type(screen.getByLabelText('Пароль'), 'new-secret');
    await user.click(screen.getByRole('button', { name: 'Сохранить новую ревизию' }));
    await screen.findByRole('alert');
    expect(credentials).toEqual([{ username: 'admin', password: 'new-secret' }]);
  });

  it('скрытые неверные настройки High не блокируют сохранение Low', async () => {
    const user = userEvent.setup();
    let sent: { config: { interaction_level: string; high?: unknown } } | undefined;
    server.use(http.post('/api/v1/profiles', async ({ request }) => {
      sent = await request.json() as typeof sent;
      return HttpResponse.json(profilesFixture.find((p) => p.config.interaction_level === 'low'), { status: 201 });
    }));
    open('/profiles/new');
    await user.type(await screen.findByLabelText('Название'), 'Переход уровня');
    await user.selectOptions(screen.getByLabelText('Уровень взаимодействия'), 'high');
    await user.clear(screen.getByLabelText('CPU'));
    await user.type(screen.getByLabelText('CPU'), '4');
    await user.selectOptions(screen.getByLabelText('Уровень взаимодействия'), 'low');
    expect(screen.queryByLabelText('CPU')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Создать профиль' }));
    await waitFor(() => expect(sent?.config.interaction_level).toBe('low'));
    expect(sent?.config.high).toBeUndefined();
  });

  it('список профилей ведёт в редактор', async () => {
    const user = userEvent.setup();
    open('/profiles');
    await user.click(await screen.findByRole('link', { name: 'Тестовый Linux-сервер' }));
    expect(await screen.findByLabelText('Название')).toHaveValue('Тестовый Linux-сервер');
  });
});
