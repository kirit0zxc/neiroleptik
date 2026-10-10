// @vitest-environment happy-dom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { DEMO_SESSION, replayHandlers } from '../../mocks/replay';
import { SessionPage } from './ReplayPage';

const server = setupServer(...replayHandlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); vi.unstubAllGlobals(); });
afterAll(() => server.close());

function open(id: string, reduced = true) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: reduced && q.includes('reduce'), media: q }));
  render(
    <MemoryRouter initialEntries={[`/sessions/${id}`]}>
      <Routes><Route path="/sessions/:id" element={<SessionPage />} /></Routes>
    </MemoryRouter>,
  );
}
const screenText = () => screen.getByLabelText('Текст записи').textContent ?? '';

describe('экран записи сессии (F15)', () => {
  it('reduced-motion: без автопуска; «В конец» показывает весь текст без управляющих последовательностей', async () => {
    open(DEMO_SESSION);
    expect(await screen.findByRole('button', { name: 'Пуск' })).toBeInTheDocument();
    expect(screenText()).not.toContain('whoami');
    await userEvent.click(screen.getByRole('button', { name: 'В конец' }));
    const text = screenText();
    expect(text).toContain('whoami');
    expect(text).toContain('Сохранение в: «/tmp/x.sh»');
    expect(text).toContain('[пропуск данных]');
    expect(text).toContain('logout');
    expect(text.includes('\u001b')).toBe(false);
    // Текст гиперссылки остаётся, адрес из OSC 8 — нет (сам адрес есть во вводе wget).
    expect(text).toContain('ссылка загружена');
    expect(text).not.toContain('8;;');
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('00:11 / 00:11')).toBeInTheDocument();
    // Направления подписаны для читалок.
    expect(screen.getAllByText('Ввод атакующего:', { exact: false }).length).toBeGreaterThan(0);
  });

  it('«В начало» скрывает показанное', async () => {
    open(DEMO_SESSION);
    await userEvent.click(await screen.findByRole('button', { name: 'В конец' }));
    await userEvent.click(screen.getByRole('button', { name: 'В начало' }));
    expect(screenText()).not.toContain('whoami');
    expect(screen.getByText('00:00 / 00:11')).toBeInTheDocument();
  });

  it('без reduced-motion воспроизведение начинается само', async () => {
    open(DEMO_SESSION, false);
    expect(await screen.findByRole('button', { name: 'Пауза' })).toBeInTheDocument();
  });

  it('нет записи — понятное сообщение', async () => {
    open('missing');
    expect(await screen.findByText('Запись сессии не найдена.')).toBeInTheDocument();
  });

  it('у низа ленты экран следует за записью и после большого блока вывода', async () => {
    open(DEMO_SESSION);
    const el = await screen.findByLabelText('Текст записи');
    // happy-dom не считает раскладку: высоту содержимого задаём по длине текста.
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 100 });
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 100 + (el.textContent?.length ?? 0) * 10 });
    let top = 0;
    Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v; } });
    await userEvent.click(screen.getByRole('button', { name: 'В конец' }));
    expect(top).toBe(el.scrollHeight);
  });
});
