// @vitest-environment happy-dom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); vi.unstubAllEnvs(); vi.restoreAllMocks(); });
afterAll(() => server.close());

function open(role: 'admin' | 'operator' | 'viewer' = 'operator') {
  resetMockSession(true, role);
  window.history.pushState({}, '', '/alerts');
  render(<App client={makeQueryClient()} />);
}
const panel = async (title: string) => screen.findByRole('region', { name: title });

describe('экран «Расследование» (F16–F18)', () => {
  beforeEach(() => { vi.stubEnv('VITE_API_MOCK', '1'); });

  it('алерт: основание — события по ссылкам, недоступное и усечение', async () => {
    const user = userEvent.setup();
    open();
    const alerts = await panel('Алерты');
    expect(await within(alerts).findByText('7 неудачных входов за 60 с с 192.0.2.23')).toBeInTheDocument();
    await user.click(within(alerts).getByRole('button', { name: 'Основание' }));
    expect(await within(alerts).findByText('Список неполный: показано ссылок — 2, событий всего — 7.')).toBeInTheDocument();
    expect(await within(alerts).findByText('Попытка входа')).toBeInTheDocument();
    expect(within(alerts).getByText('событие недоступно')).toBeInTheDocument();
    // Пароль не выводится ни в каком виде (S4).
    expect(alerts.textContent).not.toContain('hunter2');
  });

  it('медовый токен: срабатывания и их основание, значения токена нет', async () => {
    const user = userEvent.setup();
    open();
    const tokens = await panel('Медовые токены');
    expect(await within(tokens).findByText('сработал: 1')).toBeInTheDocument();
    await user.click(within(tokens).getByRole('button', { name: 'Срабатывания' }));
    expect(await within(tokens).findByText(/198\.51\.100\.7:51345/)).toBeInTheDocument();
    await user.click(within(tokens).getByRole('button', { name: 'Основание' }));
    expect(await within(tokens).findByText('HTTP-запрос')).toBeInTheDocument();
  });

  it('артефакты: имя атакующего — текстом; скачать — только доступный, оператору', async () => {
    open('operator');
    const art = await panel('Артефакты');
    expect(await within(art).findByText('<img src=x onerror=alert(1)>.bin')).toBeInTheDocument();
    expect(art.querySelector('img')).toBeNull();
    // Адрес источника — текстом, ссылок на экране артефактов нет (S3).
    expect(within(art).getAllByText('http://203.0.113.9/x.sh').length).toBeGreaterThan(0);
    expect(within(art).queryAllByRole('link')).toHaveLength(0);
    expect(within(art).getAllByRole('button', { name: /^Скачать / })).toHaveLength(1);
  });

  it('наблюдатель не видит скачивания', async () => {
    open('viewer');
    const art = await panel('Артефакты');
    await within(art).findByText('payload.bin');
    expect(within(art).queryByRole('button', { name: /^Скачать / })).toBeNull();
  });

  it('скачивание: файл сохраняется; 409 — понятное сообщение', async () => {
    const user = userEvent.setup();
    let blob: Blob | null = null;
    const createUrl = vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => { blob = b as Blob; return 'blob:mock'; });
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    let saved: { download: string; href: string } | null = null;
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      saved = { download: this.download, href: this.href };
    });
    open('admin');
    const art = await panel('Артефакты');
    await user.click(await within(art).findByRole('button', { name: 'Скачать x.sh' }));
    await waitFor(() => expect(saved).not.toBeNull());
    expect(createUrl).toHaveBeenCalledTimes(1);
    // Имя — от центра, не от атакующего; тип — octet-stream: сохранить, не открыть (S5).
    expect(saved!.download).toMatch(/^honeyforge-artifact-[0-9a-f-]+\.bin$/);
    expect(saved!.href).toBe('blob:mock');
    expect((blob as Blob | null)?.type).toBe('application/octet-stream');

    server.use(http.get('/api/v1/artifacts/:id/content', () =>
      HttpResponse.json({ detail: 'Файл ещё проверяется', code: 'ARTIFACT_PENDING' }, { status: 409 })));
    await user.click(within(art).getByRole('button', { name: 'Скачать x.sh' }));
    expect(await within(art).findByRole('alert')).toHaveTextContent('Файл ещё не готов — проверьте позже.');
  });

  it('экспорт IoC: по умолчанию 24 часа, обе даты в запросе; неверный период — кнопка недоступна', async () => {
    const user = userEvent.setup();
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => 'blob:x');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    let query: URLSearchParams | null = null;
    server.use(http.get('/api/v1/iocs.csv', ({ request }) => { query = new URL(request.url).searchParams; return undefined; }));
    open();
    const ioc = await panel('Экспорт IoC');
    await user.click(within(ioc).getByRole('button', { name: 'Скачать CSV' }));
    expect(await within(ioc).findByText(/^Файл honeyforge-iocs-\d{8}T\d{6}Z\.csv сохранён\.$/)).toBeInTheDocument();
    const q = query as URLSearchParams | null;
    expect(Date.parse(q!.get('to')!) - Date.parse(q!.get('from')!)).toBe(86_400_000);

    const [fromInput] = within(ioc).getAllByLabelText(/^(С|По)/) as HTMLInputElement[];
    await user.clear(fromInput!);
    await user.type(fromInput!, '2026-01-01T00:00');
    expect(within(ioc).getByRole('alert')).toHaveTextContent('Период — не больше 31 дня.');
    expect(within(ioc).getByRole('button', { name: 'Скачать CSV' })).toBeDisabled();
  });

  it('422 INVALID_CURSOR на «Показать ещё» — список перечитывается с начала', async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`, deployment_id: '00000000-0000-0000-0000-0000000000d1',
      kind: 'url', label: `token-${i}`, created_at: '2026-10-10T00:00:00Z', last_triggered_at: null, trigger_count: 0,
    }));
    let firstPages = 0;
    server.use(http.get('/api/v1/honeytokens', ({ request }) => {
      if (new URL(request.url).searchParams.get('cursor')) {
        return HttpResponse.json({ detail: 'Курсор устарел', code: 'INVALID_CURSOR' }, { status: 422 });
      }
      firstPages++;
      return HttpResponse.json({ items: many.slice(0, 50), next_cursor: 'c50' });
    }));
    const user = userEvent.setup();
    open();
    const tokens = await panel('Медовые токены');
    await within(tokens).findByText('token-0');
    await user.click(within(tokens).getByRole('button', { name: 'Показать ещё' }));
    await waitFor(() => expect(firstPages).toBe(2));
    expect(await within(tokens).findByText('token-0')).toBeInTheDocument();
  });

  it('без флага — заглушка', () => {
    vi.stubEnv('VITE_API_MOCK', '0');
    vi.stubEnv('VITE_INVESTIGATION_ENABLED', '0');
    vi.stubEnv('VITE_TELEMETRY_ENABLED', '0');
    open();
    return screen.findByText(/Экран в работе/).then((el) => expect(el).toBeInTheDocument());
  });

  it('сбой «Показать ещё» виден, загруженное остаётся, «Повторить» догружает', async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`, deployment_id: '00000000-0000-0000-0000-0000000000d1',
      kind: 'url', label: `t-${i}`, created_at: '2026-10-10T00:00:00Z', last_triggered_at: null, trigger_count: 0,
    }));
    let fail = true;
    server.use(http.get('/api/v1/honeytokens', ({ request }) => {
      if (!new URL(request.url).searchParams.get('cursor')) return HttpResponse.json({ items: many.slice(0, 50), next_cursor: 'c50' });
      if (fail) return HttpResponse.json({ detail: 'Центр недоступен' }, { status: 503 });
      return HttpResponse.json({ items: many.slice(50), next_cursor: null });
    }));
    const user = userEvent.setup();
    open();
    const tokens = await panel('Медовые токены');
    await within(tokens).findByText('t-0');
    await user.click(within(tokens).getByRole('button', { name: 'Показать ещё' }));
    // 5xx приложение повторяет само (2 раза с паузой) — ждём дольше.
    expect(await within(tokens).findByRole('alert', {}, { timeout: 8000 })).toHaveTextContent('Не удалось загрузить ещё: Центр недоступен');
    expect(within(tokens).getByText('t-0')).toBeInTheDocument();
    fail = false;
    await user.click(within(tokens).getByRole('button', { name: 'Повторить' }));
    expect(await within(tokens).findByText('t-59')).toBeInTheDocument();
  }, 20000);
});
