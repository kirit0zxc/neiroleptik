// @vitest-environment happy-dom
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { api, ApiError, setCsrfToken, setUnauthorizedHandler } from './client';

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); setCsrfToken(null); });
afterAll(() => server.close());

describe('API-клиент (раздел 4)', () => {
  it('X-CSRF-Token только на изменяющих запросах и только если токен известен', async () => {
    const seen: Array<string | null> = [];
    server.use(
      http.get('/x', ({ request }) => { seen.push(request.headers.get('X-CSRF-Token')); return HttpResponse.json({}); }),
      http.post('/x', ({ request }) => { seen.push(request.headers.get('X-CSRF-Token')); return HttpResponse.json({}); }),
    );
    await api('/x', { method: 'POST', body: '{}' });
    setCsrfToken('tok-1');
    await api('/x');
    await api('/x', { method: 'POST', body: '{}' });
    expect(seen).toEqual([null, null, 'tok-1']);
  });

  it('строковый detail становится текстом ошибки, логика — по статусу', async () => {
    server.use(http.patch('/p', () => HttpResponse.json({ detail: 'Профиль уже изменён: перечитайте текущую версию' }, { status: 409 })));
    const err = await api('/p', { method: 'PATCH', body: '{}' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).message).toBe('Профиль уже изменён: перечитайте текущую версию');
  });

  it('обрыв тела успешного ответа — неопределённый результат (статус 0), а не обычная ошибка', async () => {
    server.use(http.post('/t', () => new HttpResponse('{"job_id":"j', { status: 202, headers: { 'Content-Type': 'application/json' } })));
    const err = await api('/t', { method: 'POST', body: '{}' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
    expect((err as ApiError).message).toBe('Ответ сервера получен не полностью — результат неизвестен');
  });

  it('код ошибки из ErrorResponse.code сохраняется для логики экрана', async () => {
    server.use(
      http.post('/c', () => HttpResponse.json({ detail: 'Перечитайте поколение', code: 'STALE_GENERATION' }, { status: 409 })),
      http.post('/n', () => HttpResponse.json({ detail: 'Без кода' }, { status: 409 })),
    );
    const withCode = await api('/c', { method: 'POST', body: '{}' }).catch((e: unknown) => e) as ApiError;
    expect(withCode.code).toBe('STALE_GENERATION');
    expect(withCode.message).toBe('Перечитайте поколение');
    const noCode = await api('/n', { method: 'POST', body: '{}' }).catch((e: unknown) => e) as ApiError;
    expect(noCode.code).toBeNull();
  });

  it('422: список ошибок раскладывается по путям полей без «body»', async () => {
    server.use(http.post('/p', () => HttpResponse.json({ detail: [
      { loc: ['body', 'config', 'services', 0, 'port'], type: 'value_error', msg: 'порт занят' },
      { loc: ['body', 'config', 'name'], type: 'missing', msg: 'обязательно' },
      { loc: 'мусор', msg: 1 },
    ] }, { status: 422 })));
    const err = await api('/p', { method: 'POST', body: '{}' }).catch((e: unknown) => e) as ApiError;
    expect(err.status).toBe(422);
    expect(err.fields).toEqual([
      { path: ['config', 'services', 0, 'port'], message: 'порт занят' },
      { path: ['config', 'name'], message: 'обязательно' },
    ]);
  });

  it('401 вызывает обработчик выхода, но не при неверном пароле', async () => {
    const fn = vi.fn();
    setUnauthorizedHandler(fn);
    server.use(
      http.get('/api/v1/hosts', () => HttpResponse.json({ detail: 'нет сессии' }, { status: 401 })),
      http.post('/api/v1/auth/login', () => HttpResponse.json({ detail: 'Неверный логин или пароль' }, { status: 401 })),
    );
    await api('/api/v1/auth/login', { method: 'POST', body: '{}' }).catch(() => {});
    expect(fn).not.toHaveBeenCalled();
    await api('/api/v1/hosts').catch(() => {});
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('тело не JSON и сетевой сбой — понятные сообщения', async () => {
    server.use(http.get('/e', () => new HttpResponse('<html>502</html>', { status: 502 })));
    await expect(api('/e')).rejects.toMatchObject({ status: 502, message: 'Ошибка сервера — попробуйте позже' });
    server.use(http.get('/n', () => HttpResponse.error()));
    await expect(api('/n')).rejects.toMatchObject({ status: 0, message: 'Нет связи с сервером' });
  });
});
