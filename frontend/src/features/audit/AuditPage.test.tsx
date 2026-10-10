// @vitest-environment happy-dom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function open(path = '/audit') {
  window.history.pushState({}, '', path);
  render(<App client={makeQueryClient()} />);
}
const rowOf = async (text: string) => (await screen.findByText(text)).closest('tr')!;
const entry = (i: number) => ({
  id: `e-${i}`, actor_id: 'u-admin-1', action: 'auth.login', target_id: null, details: {}, created_at: '2026-10-10T09:00:00Z',
});

describe('журнал аудита (F19)', () => {
  it('администратор видит записи: кто, действие, объект, подробности, результат', async () => {
    resetMockSession(true, 'admin');
    open();
    const ack = await rowOf('агент подтвердил команду');
    expect(within(ack).getByText('хост runtime-01')).toBeInTheDocument();
    expect(within(ack).getByText('итог: успешно · поколение 1')).toBeInTheDocument();
    expect(within(ack).getByText('успех')).toBeInTheDocument();

    const planned = await rowOf('ловушка назначена');
    expect(within(planned).getByRole('link', { name: 'ловушка decoy-lo' })).toHaveAttribute('href', '/fleet');

    const updated = await rowOf('профиль изменён');
    expect(within(updated).getByRole('link', { name: 'Веб-панель служебного устройства' })).toHaveAttribute('href', '/profiles/p-web-panel');
    expect(within(updated).getByText('ревизия r2')).toBeInTheDocument();

    const failed = await rowOf('неудачный вход');
    expect(within(failed).getByText('система')).toBeInTheDocument();
    expect(within(failed).getByText('логин: root')).toBeInTheDocument();
    expect(within(failed).getByText('ошибка')).toBeInTheDocument();

    expect(within(await rowOf('команда истекла')).getByText('код: DELIVERY_EXPIRED')).toBeInTheDocument();
    expect(within(await rowOf('вход в систему')).getByText('вы (admin)')).toBeInTheDocument();
  });

  it('оператор видит понятный отказ, а запрос к журналу не уходит', async () => {
    resetMockSession(true);
    let calls = 0;
    server.use(http.get('/api/v1/audit', () => { calls += 1; return HttpResponse.json([]); }));
    open();
    expect(await screen.findByText('Журнал аудита доступен только администратору.')).toBeInTheDocument();
    expect(calls).toBe(0);
  });

  it('403 от API — тот же понятный отказ', async () => {
    resetMockSession(true, 'admin');
    server.use(http.get('/api/v1/audit', () => HttpResponse.json({ detail: 'Недостаточно прав' }, { status: 403 })));
    open();
    expect(await screen.findByText('Журнал аудита доступен только администратору.')).toBeInTheDocument();
  });

  it('подробности атакующего — только текст (S1)', async () => {
    resetMockSession(true, 'admin');
    server.use(http.get('/api/v1/audit', () => HttpResponse.json([{
      ...entry(1), actor_id: null, action: 'auth.login_failed', details: { username: '<img src=x onerror=alert(1)>' },
    }])));
    open();
    expect(await screen.findByText('логин: <img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(document.querySelector('td img')).toBeNull();
  });

  it('пагинация: «Старее» при полной странице, offset в запросе, страница в адресе', async () => {
    const user = userEvent.setup();
    resetMockSession(true, 'admin');
    const offsets: string[] = [];
    server.use(http.get('/api/v1/audit', ({ request }) => {
      const q = new URL(request.url).searchParams;
      offsets.push(`${q.get('limit')}/${q.get('offset')}`);
      return HttpResponse.json(q.get('offset') === '0' ? Array.from({ length: 50 }, (_, i) => entry(i)) : [entry(99)]);
    }));
    open();
    await screen.findAllByText('вход в систему');
    expect(screen.getByRole('button', { name: '← Новее' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Старее →' }));
    expect(await screen.findByText('Страница 2')).toBeInTheDocument();
    expect(window.location.search).toBe('?page=1');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Старее →' })).toBeDisabled());
    expect(offsets).toEqual(['50/0', '50/50']);
  });

  it('ошибка загрузки — сообщение и «Повторить»', async () => {
    const user = userEvent.setup();
    resetMockSession(true, 'admin');
    let fail = true;
    server.use(http.get('/api/v1/audit', () => (fail ? HttpResponse.json({ detail: 'Журнал недоступен' }, { status: 400 }) : HttpResponse.json([entry(1)]))));
    open();
    expect(await screen.findByText('Журнал недоступен')).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('вход в систему')).toBeInTheDocument();
  });
});
