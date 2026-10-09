// @vitest-environment happy-dom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import type { Profile, Revision } from '../../shared/api/types';

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

/** История с изменением уровня и портов проверяет выбор снимка, а не latest. */
const evolvingProfile: Profile = {
  id: 'p-evolving', name: 'Меняющийся профиль', description: '', archived: false,
  revision: 2, created_at: '2026-10-09T11:00:00Z',
  config: {
    interaction_level: 'high', hostname: 'new-vm', credential_usernames: ['admin'],
    services: [{ protocol: 'ssh', port: 2224, banner: 'VM' }],
    high: { template: 'ubuntu-ssh-v1', ttl_seconds: 300, cpu: 1, memory_mb: 1024 },
  },
};
const evolvingRevisions: Revision[] = [
  { revision: 1, created_at: '2026-10-08T11:00:00Z', config: {
    interaction_level: 'low', hostname: 'old-camera', credential_usernames: [],
    services: [{ protocol: 'http', port: 8081, banner: 'Camera' }],
  } },
  { revision: 2, created_at: evolvingProfile.created_at, config: evolvingProfile.config },
];

function serveEvolvingProfile(revisions = evolvingRevisions, profile = evolvingProfile) {
  server.use(
    http.get('/api/v1/profiles', () => HttpResponse.json([profile])),
    http.get('/api/v1/profiles/p-evolving/revisions', () => HttpResponse.json(revisions)),
  );
}

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
    await waitFor(() => expect(screen.getByLabelText('Ревизия')).toHaveValue('4'));
    await waitFor(() => expect(next()).toBeEnabled());
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

  it('High доступен только на хосте, поддерживающем уровень high', async () => {
    const user = userEvent.setup();
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    // Профиль доступен для поиска совместимой старой ревизии, но назначение — нет.
    await user.click(screen.getByRole('radio', { name: /Настоящая Linux VM/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('High нужна поддержка настоящей VM — на этом хосте её нет');
    expect(next()).toBeDisabled();

    await user.click(screen.getByRole('button', { name: 'Назад' }));
    await user.click(screen.getByRole('radio', { name: /host-runtime-02/ }));
    await user.click(next());
    await waitFor(() => expect(next()).toBeEnabled());
  });

  it('«Назад» не теряет выбор; можно выбрать старую ревизию', async () => {
    const user = userEvent.setup();
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    await user.click(screen.getByRole('radio', { name: /Веб-панель/ }));
    await screen.findByRole('option', { name: /r1/ });
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
    await waitFor(() => expect(next()).toBeEnabled());
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

  it('несовместимая latest не мешает назначить старую ревизию: сводка и запрос используют r1', async () => {
    const user = userEvent.setup();
    serveEvolvingProfile();
    let body: unknown;
    server.use(http.post('/api/v1/deployments', async ({ request }) => {
      body = await request.json();
      return HttpResponse.json({ id: 'planned-old', host_id: 'host-runtime-01', profile_id: 'p-evolving', revision: 1, state: 'planned', created_at: evolvingProfile.created_at });
    }));
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    expect(screen.getByRole('radio', { name: /Меняющийся профиль/ })).toBeEnabled();
    await user.click(screen.getByRole('radio', { name: /Меняющийся профиль/ }));
    await screen.findByRole('option', { name: /r1/ });
    expect(next()).toBeDisabled();
    await user.selectOptions(screen.getByLabelText('Ревизия'), '1');
    expect(next()).toBeEnabled();
    await user.click(next());
    expect(screen.getByText('Low')).toBeInTheDocument();
    expect(screen.getByText('http/8081')).toBeInTheDocument();
    expect(screen.getByText('old-camera')).toBeInTheDocument();
    expect(screen.queryByText('ssh/2224')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Запланировать' }));
    await screen.findByText(paragraph(/^Ловушка planned-old запланирована/));
    expect(body).toEqual({ host_id: 'host-runtime-01', profile_id: 'p-evolving', revision: 1 });
  });

  it('несовместимая старая ревизия запрещена даже при совместимой latest', async () => {
    const user = userEvent.setup();
    const profile = { ...evolvingProfile, config: evolvingRevisions[0]!.config };
    serveEvolvingProfile([
      { ...evolvingRevisions[1]!, revision: 1 },
      { ...evolvingRevisions[0]!, revision: 2 },
    ], profile);
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-01/ }));
    await user.click(next());
    await user.click(screen.getByRole('radio', { name: /Меняющийся профиль/ }));
    await waitFor(() => expect(next()).toBeEnabled());
    await user.selectOptions(screen.getByLabelText('Ревизия'), '1');
    expect(next()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('High нужна поддержка настоящей VM');
  });

  it('не подменяет загружаемую ревизию конфигурацией профиля', async () => {
    const user = userEvent.setup();
    serveEvolvingProfile();
    let release!: () => void;
    const waiting = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get('/api/v1/profiles/p-evolving/revisions', async () => {
      await waiting;
      return HttpResponse.json(evolvingRevisions);
    }));
    try {
      open();
      await user.click(await screen.findByRole('radio', { name: /host-runtime-02/ }));
      await user.click(next());
      await user.click(screen.getByRole('radio', { name: /Меняющийся профиль/ }));
      expect(screen.getByRole('status')).toHaveTextContent('Загружаем ревизии');
      expect(next()).toBeDisabled();
      expect(screen.getByLabelText('Ревизия')).toBeDisabled();
    } finally { release(); }
    await waitFor(() => expect(next()).toBeEnabled());
  });

  it('ошибка чтения ревизий блокирует переход; повтор возвращает доступ', async () => {
    const user = userEvent.setup();
    serveEvolvingProfile();
    server.use(http.get('/api/v1/profiles/p-evolving/revisions', () => HttpResponse.json({ detail: 'История недоступна' }, { status: 404 })));
    open();
    await user.click(await screen.findByRole('radio', { name: /host-runtime-02/ }));
    await user.click(next());
    await user.click(screen.getByRole('radio', { name: /Меняющийся профиль/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('История недоступна');
    expect(next()).toBeDisabled();
    serveEvolvingProfile();
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    await waitFor(() => expect(next()).toBeEnabled());
  });
});
