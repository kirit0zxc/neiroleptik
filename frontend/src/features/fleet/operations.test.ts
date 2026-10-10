// @vitest-environment happy-dom
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../shared/api/client';
import { Operations } from './operations';

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const settled = async (ops: Operations, id: string) => vi.waitFor(() => expect(ops.get(id).sending).toBe(false));

describe('операции ловушек (Operations)', () => {
  it('202 без job_id (пустое тело) — результат неизвестен, можно повторить тем же ключом', async () => {
    const keys: string[] = [];
    let empty = true;
    server.use(http.post('/api/v1/deployments/:id/commands', async ({ request }) => {
      keys.push(((await request.json()) as { idempotency_key: string }).idempotency_key);
      if (empty) { empty = false; return new HttpResponse('', { status: 202 }); }
      return HttpResponse.json({ job_id: 'job-1' }, { status: 202 });
    }));
    const ops = new Operations();
    ops.begin('d1', 'start', 0);
    await settled(ops, 'd1');
    expect(ops.get('d1').error).toBeInstanceOf(ApiError);
    expect((ops.get('d1').error as ApiError).status).toBe(0);
    ops.retry('d1');
    await vi.waitFor(() => expect(ops.get('d1').jobId).toBe('job-1'));
    expect(keys[1]).toBe(keys[0]);
  });

  it('поздний ответ после «Отменить» не перезаписывает состояние', async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    server.use(http.post('/api/v1/deployments/:id/commands', async () => {
      await gate;
      return HttpResponse.json({ job_id: 'late-job' }, { status: 202 });
    }));
    const refresh = vi.fn();
    const ops = new Operations(refresh);
    ops.begin('d1', 'stop', 3);
    ops.cancel('d1');
    expect(refresh).toHaveBeenCalledOnce();
    release();
    await new Promise((r) => setTimeout(r, 50));
    expect(ops.get('d1').jobId).toBeNull();
    expect(ops.get('d1').action).toBeNull();
  });

  it('отказ 409 — перечитать данные ловушек', async () => {
    server.use(http.post('/api/v1/deployments/:id/commands', () =>
      HttpResponse.json({ detail: 'занято', code: 'COMMAND_BUSY' }, { status: 409 })));
    const refresh = vi.fn();
    const ops = new Operations(refresh);
    ops.begin('d1', 'start', 0);
    await settled(ops, 'd1');
    expect(refresh).toHaveBeenCalledOnce();
    expect((ops.get('d1').error as ApiError).code).toBe('COMMAND_BUSY');
  });
});
