import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { SessionEvents } from './SessionEvents';

const event = (id: string, type: string, data: Record<string, unknown>) => ({
  event_id: id, agent_id: 'agent', session_id: 'session', type, data,
  occurred_at: '2026-10-16T00:00:00Z', source: null,
});
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('история сессии', () => {
  it('передаёт фильтр сессии и показывает команду текстом, не раскрывая пароль', async () => {
    let params: URLSearchParams | undefined;
    server.use(http.get('/api/v1/events', ({ request }) => {
      params = new URL(request.url).searchParams;
      return HttpResponse.json({ items: [event('1', 'auth.attempt', { success: false, password: 'SECRET' }), event('2', 'session.command', { input: '<script>alert(1)</script>', truncated: true })], next_cursor: null });
    }));
    render(<SessionEvents id="session /?" />);
    expect(await screen.findByText('Вход отклонён')).toBeInTheDocument();
    expect(screen.getByText('<script>alert(1)</script> [обрезано]')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('SECRET');
    expect(document.querySelector('script')).toBeNull();
    expect(params?.get('session_id')).toBe('session /?');
    expect(params?.get('order')).toBe('asc');
    expect(params?.get('sort')).toBe('occurred_at');
    expect(screen.getAllByText('неизвестен')).toHaveLength(2);
  });

  it('переключает страницы по непрозрачному cursor и возвращается к первой', async () => {
    server.use(http.get('/api/v1/events', ({ request }) => HttpResponse.json({
      items: [event('1', 'session.command', { input: new URL(request.url).searchParams.has('cursor') ? 'second' : 'first' })],
      next_cursor: new URL(request.url).searchParams.has('cursor') ? null : 'opaque+/=',
    })));
    render(<SessionEvents id="session" />);
    await screen.findByText('first');
    await userEvent.click(screen.getByRole('button', { name: 'Следующие события' }));
    await screen.findByText('second');
    expect(screen.queryByText('first')).toBeNull();
    expect(screen.getByRole('button', { name: 'Следующие события' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Предыдущие события' }));
    await screen.findByText('first');
  });

  it('показывает отказ и позволяет повторить чтение', async () => {
    server.use(http.get('/api/v1/events', () => new HttpResponse(null, { status: 503 })));
    render(<SessionEvents id="session" />);
    await screen.findByText('Не удалось загрузить события сессии.');
    server.use(http.get('/api/v1/events', () => HttpResponse.json({ items: [], next_cursor: null })));
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    await screen.findByText('События этой сессии пока не опубликованы.');
  });

  it('обновление возвращается к первой странице и перечитывает данные', async () => {
    let requests = 0;
    server.use(http.get('/api/v1/events', () => {
      requests++;
      return HttpResponse.json({ items: [event('1', 'session.command', { input: `read-${requests}` })], next_cursor: 'next' });
    }));
    render(<SessionEvents id="session" />);
    await screen.findByText('read-1');
    await userEvent.click(screen.getByRole('button', { name: 'Следующие события' }));
    await screen.findByText('read-2');
    await userEvent.click(screen.getByRole('button', { name: 'Обновить события' }));
    await waitFor(() => expect(screen.getByText('Страница 1')).toBeInTheDocument());
    await screen.findByText('read-3');
  });
});
