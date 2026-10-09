// @vitest-environment happy-dom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { handlers, resetMockSession } from '../mocks/handlers';
import { App, makeQueryClient } from './App';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); });
afterAll(() => server.close());

function go(path: string) { window.history.pushState({}, '', path); }

describe('приложение на моках (F1, F3, F5, S1, S4)', () => {
  beforeEach(() => { vi.stubEnv('VITE_API_MOCK', '1'); resetMockSession(false); go('/'); });

  it.each(['/', '/events'])('настоящий control-режим %s не вызывает будущие API или WS', async (path) => {
    vi.stubEnv('VITE_API_MOCK', '0');
    vi.stubEnv('VITE_TELEMETRY_ENABLED', '0');
    resetMockSession(true);
    const connect = vi.fn();
    vi.stubGlobal('WebSocket', connect);
    let futureRequests = 0;
    server.use(
      http.get('/api/v1/analytics/summary', () => { futureRequests++; return HttpResponse.json({}); }),
      http.get('/api/v1/events', () => { futureRequests++; return HttpResponse.json({ items: [] }); }),
    );
    go(path);
    render(<App client={makeQueryClient()} />);
    expect(await screen.findByRole('heading', { name: path === '/' ? 'Парк ловушек' : 'События' })).toBeInTheDocument();
    if (path === '/') expect(window.location.pathname).toBe('/fleet');
    else expect(screen.getByText(/подключение серверной телеметрии/)).toBeInTheDocument();
    expect(connect).not.toHaveBeenCalled();
    expect(futureRequests).toBe(0);
  });

  it('без сессии — вход; неверный пароль — ошибка; после входа — на исходную страницу', async () => {
    const user = userEvent.setup();
    go('/fleet');
    render(<App client={makeQueryClient()} />);

    expect(await screen.findByRole('button', { name: 'Войти' })).toBeInTheDocument();
    await user.type(screen.getByLabelText('Логин'), 'operator');
    await user.type(screen.getByLabelText('Пароль'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Войти' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Неверный логин или пароль');
    await new Promise((r) => setTimeout(r, 50));

    await user.clear(screen.getByLabelText('Пароль'));
    await user.type(screen.getByLabelText('Пароль'), 'operator');
    await user.click(screen.getByRole('button', { name: 'Войти' }));

    // Вернулись на /fleet: видно drift у Medium, а не зелёный успех.
    expect(await screen.findByRole('heading', { name: 'Парк ловушек' })).toBeInTheDocument();
    const medium = (await screen.findByText('decoy-medium-01')).closest('tr')!;
    expect(within(medium).getByText('drift: r3 → r4')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/fleet');
  });

  it('сессия истекла посреди работы (401) — переход на вход (F3)', async () => {
    resetMockSession(true);
    go('/fleet');
    server.use(http.get('/api/v1/hosts', () => HttpResponse.json({ detail: 'Требуется вход' }, { status: 401 })));
    render(<App client={makeQueryClient()} />);
    expect(await screen.findByRole('button', { name: 'Войти' })).toBeInTheDocument();
  });

  it('пустые поля входа не отправляются', async () => {
    const user = userEvent.setup();
    render(<App client={makeQueryClient()} />);
    await user.click(await screen.findByRole('button', { name: 'Войти' }));
    expect(await screen.findByText('Введите логин')).toBeInTheDocument();
    expect(screen.getByText('Введите пароль')).toBeInTheDocument();
  });

  it('лента: XSS из запроса атакующего видна текстом, пароль замаскирован', async () => {
    resetMockSession(true);
    go('/events');
    render(<App client={makeQueryClient()} />);

    const rows = await screen.findAllByTestId('event-row');
    expect(rows).toHaveLength(3);
    expect(screen.getByText('GET /<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getByText('root / h•••••2 успешно')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('hunter2');
  });
});
