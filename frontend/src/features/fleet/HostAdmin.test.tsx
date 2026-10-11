// @vitest-environment happy-dom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); vi.unstubAllGlobals(); });
afterAll(() => server.close());

function open(role: 'admin' | 'operator') {
  resetMockSession(true, role);
  window.history.pushState({}, '', '/fleet');
  render(<App client={makeQueryClient()} />);
}
const hostRow = async (name: string) => (await screen.findByText(name)).closest('tr')!;

describe('регистрация агента и отзыв хоста (F4)', () => {
  it('StrictMode: одно открытие — один POST, повторное открытие выдаёт новый токен', async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(http.post('/api/v1/enrollments', () => {
      calls++;
      return HttpResponse.json({ id: String(calls), enrollment_token: `strict-${calls}`, expires_at: new Date(Date.now() + 900_000).toISOString() }, { status: 201 });
    }));
    resetMockSession(true, 'admin');
    window.history.pushState({}, '', '/fleet');
    render(<StrictMode><App client={makeQueryClient()} /></StrictMode>);
    for (const expected of [1, 2]) {
      await user.click(await screen.findByRole('button', { name: '+ Добавить хост' }));
      expect(await screen.findByDisplayValue(`strict-${expected}`)).toBeInTheDocument();
      expect(calls).toBe(expected);
      await user.click(screen.getByRole('button', { name: 'Закрыть' }));
    }
  });

  it.each(['resolve', 'reject'] as const)('старое копирование (%s) не меняет состояние нового токена', async (outcome) => {
    const user = userEvent.setup();
    let resolve!: () => void;
    let reject!: (error: Error) => void;
    const pending = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
    const writeText = vi.fn(() => pending);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    open('admin');
    await user.click(await screen.findByRole('button', { name: '+ Добавить хост' }));
    const input = await screen.findByRole('textbox', { name: 'Токен регистрации' }) as HTMLInputElement;
    const old = input.value;
    await user.click(screen.getByRole('button', { name: 'Скопировать' }));
    expect(writeText).toHaveBeenCalledWith(old);
    await user.click(screen.getByRole('button', { name: 'Выдать новый токен' }));
    const current = await screen.findByRole('textbox', { name: 'Токен регистрации' }) as HTMLInputElement;
    expect(current.value).not.toBe(old);
    current.setSelectionRange(0, 0);
    await act(async () => {
      if (outcome === 'resolve') resolve();
      else reject(new Error('denied'));
      await pending.catch(() => {});
    });
    expect(screen.queryByText(/Скопировано|скопируйте вручную/)).toBeNull();
    expect(current.selectionEnd).toBe(0);
  });

  it('оператор не видит «Добавить хост» и «Отозвать»', async () => {
    open('operator');
    await hostRow('runtime-01');
    expect(screen.queryByRole('button', { name: '+ Добавить хост' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Отозвать хост/ })).toBeNull();
  });

  it('admin получает токен один раз: срок, копирование, команда без токена', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    open('admin');
    await user.click(await screen.findByRole('button', { name: '+ Добавить хост' }));
    const dialog = await screen.findByRole('dialog', { name: 'Регистрация агента' });
    const input = await within(dialog).findByRole('textbox', { name: 'Токен регистрации' });
    const token = (input as HTMLInputElement).value;
    expect(token).toMatch(/^hf_enroll_/);
    expect(within(dialog).getByText(/осталось 15 минут/)).toBeInTheDocument();
    expect(within(dialog).getByText(/agent\.runtime\.executor\.enroll/).textContent).not.toContain(token);
    await user.click(within(dialog).getByRole('button', { name: 'Скопировать' }));
    expect(writeText).toHaveBeenCalledWith(token);
    expect(within(dialog).getByText(/Скопировано/)).toBeInTheDocument();
    // Новый токен — отдельный запрос и другое значение.
    await user.click(within(dialog).getByRole('button', { name: 'Выдать новый токен' }));
    await waitFor(() => expect((within(dialog).getByRole('textbox', { name: 'Токен регистрации' }) as HTMLInputElement).value).not.toBe(token));
    // Токен не сохраняется в адресе и хранилищах.
    expect(window.location.href).not.toContain('hf_enroll');
    await user.click(within(dialog).getByRole('button', { name: 'Закрыть' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByDisplayValue(/hf_enroll_/)).toBeNull();
  });

  it('ошибка выдачи — сообщение в окне, токена нет', async () => {
    const user = userEvent.setup();
    server.use(http.post('/api/v1/enrollments', () => HttpResponse.json({ detail: 'Слишком часто' }, { status: 429 })));
    open('admin');
    await user.click(await screen.findByRole('button', { name: '+ Добавить хост' }));
    const dialog = await screen.findByRole('dialog', { name: 'Регистрация агента' });
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Токен не выдан: Слишком часто');
    expect(within(dialog).queryByRole('textbox')).toBeNull();
  });

  it('отзыв: подтверждение, статус «отозван», кнопка исчезает', async () => {
    const user = userEvent.setup();
    open('admin');
    const row = await hostRow('runtime-01');
    await user.click(within(row).getByRole('button', { name: 'Отозвать хост runtime-01' }));
    const dialog = await screen.findByRole('dialog', { name: 'Отозвать хост runtime-01?' });
    expect(dialog).toHaveTextContent('Действие не отменить');
    await user.click(within(dialog).getByRole('button', { name: 'Подтвердить' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const updated = await hostRow('runtime-01');
    await waitFor(() => expect(within(updated).queryByRole('button', { name: /Отозвать/ })).toBeNull());
    expect(within(updated).getByText(/отозван/i)).toBeInTheDocument();
    // Кнопка исчезла — фокус на строке хоста, а не в body.
    expect(document.activeElement).toBe(updated);
  });

  it('сбой отзыва — ошибка в диалоге и повтор; «Отмена» ничего не меняет', async () => {
    const user = userEvent.setup();
    let fail = true;
    server.use(http.post('/api/v1/hosts/:id/revoke', () => (fail
      ? HttpResponse.json({ detail: 'Центр недоступен' }, { status: 503 }) : undefined)));
    open('admin');
    const row = await hostRow('runtime-02');
    await user.click(within(row).getByRole('button', { name: 'Отозвать хост runtime-02' }));
    let dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Подтвердить' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Центр недоступен');
    await user.click(within(dialog).getByRole('button', { name: 'Отмена' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(within(await hostRow('runtime-02')).getByRole('button', { name: 'Отозвать хост runtime-02' })).toBeInTheDocument();
    fail = false;
    await user.click(within(await hostRow('runtime-02')).getByRole('button', { name: 'Отозвать хост runtime-02' }));
    dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Подтвердить' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(within(await hostRow('runtime-02')).getByText(/отозван/i)).toBeInTheDocument();
  });

  it('буфер обмена недоступен — токен выделен для ручного копирования', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: async () => { throw new Error('denied'); } } });
    open('admin');
    await user.click(await screen.findByRole('button', { name: '+ Добавить хост' }));
    const dialog = await screen.findByRole('dialog', { name: 'Регистрация агента' });
    const input = await within(dialog).findByRole('textbox', { name: 'Токен регистрации' }) as HTMLInputElement;
    await user.click(within(dialog).getByRole('button', { name: 'Скопировать' }));
    expect(await within(dialog).findByText(/скопируйте вручную/)).toBeInTheDocument();
    expect(input.selectionEnd! - input.selectionStart!).toBe(input.value.length);
  });
});
