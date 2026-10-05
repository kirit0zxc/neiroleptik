/// <reference types="node" />
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cwd } from 'node:process';
import { ServerTTS } from './ServerTTS';
import { Voice } from './Voice';

/** Минимальный Web Audio: проверяем жизненный цикл без звука и WebGL. */
function fixture(transport?: typeof fetch, timeoutMs?: number) {
  const source = {
    buffer: null, onended: null as null | (() => void),
    connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(),
  };
  const context = {
    destination: {}, resume: vi.fn(async () => {}),
    close: vi.fn(async () => {}), decodeAudioData: vi.fn(async () => ({})),
    createBufferSource: vi.fn(() => source),
  };
  const fetch = transport ?? vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: 'token' })))
    .mockResolvedValueOnce(new Response(new Uint8Array(44), { headers: { 'Content-Type': 'audio/wav' } }));
  const provider = new ServerTTS({
    fetch, timeoutMs, createAudioContext: () => context as unknown as AudioContext,
  });
  const handlers = { onStart: vi.fn(), onAudio: vi.fn(), onEnd: vi.fn(), onError: vi.fn() };
  return { provider, source, context, fetch, handlers };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
afterEach(() => vi.useRealTimers());

describe('серверный голос', () => {
  it('запрос соответствует OpenAPI, звук передан липсинку, ресурсы закрываются', async () => {
    const f = fixture();
    const promise = f.provider.speak('  Привет  ', f.handlers);
    await flush();
    const call = vi.mocked(f.fetch).mock.calls[1];
    const spec = JSON.parse(readFileSync(resolve(cwd(), '../specs/openapi.json'), 'utf-8'));
    expect(call[0]).toBe(Object.keys(spec.paths).find((path) => spec.paths[path].post));
    expect(JSON.parse(call[1]!.body as string)).toEqual({ text: 'Привет', rate: 1 });
    expect(call[1]?.headers).toMatchObject({ 'X-CSRFToken': 'token' });
    expect(call[1]?.credentials).toBe('same-origin');
    expect(f.context.resume).toHaveBeenCalled();
    expect(f.handlers.onAudio).toHaveBeenCalledWith(f.source, f.context);
    expect(f.source.start).toHaveBeenCalledOnce();
    f.source.onended?.();
    await promise;
    expect(f.handlers.onEnd).toHaveBeenCalledOnce();
    expect(f.context.close).toHaveBeenCalledOnce();
    expect(f.source.disconnect).toHaveBeenCalledOnce();
  });

  it('новая реплика отменяет старую, поздний ответ не воспроизводится', async () => {
    let oldReply!: (response: Response) => void;
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: 'token' })))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { oldReply = resolve; }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: 'token' })))
      .mockResolvedValueOnce(new Response(new Uint8Array(44), { headers: { 'Content-Type': 'audio/wav' } }));
    const f = fixture(fetch);
    const old = f.provider.speak('Старая', f.handlers);
    await flush();
    const current = f.provider.speak('Новая', f.handlers);
    await old;
    oldReply(new Response(new Uint8Array(44), { headers: { 'Content-Type': 'audio/wav' } }));
    await flush();
    expect(f.context.decodeAudioData).toHaveBeenCalledOnce();
    expect(f.source.start).toHaveBeenCalledOnce();
    f.provider.cancel();
    await current;
    expect(f.handlers.onError).not.toHaveBeenCalled();
  });

  it('отмена во время HTTP завершает promise и отменяет signal', async () => {
    const f = fixture(vi.fn(() => new Promise<Response>(() => {})));
    const speech = f.provider.speak('Привет', f.handlers);
    const signal = vi.mocked(f.fetch).mock.calls[0][1]?.signal;
    f.provider.cancel();
    await speech;
    expect(signal?.aborted).toBe(true);
    expect(f.handlers.onEnd).toHaveBeenCalledOnce();
    expect(f.source.start).not.toHaveBeenCalled();
    expect(f.context.close).toHaveBeenCalledOnce();
  });

  it('ошибка HTTP завершает состояние речи ядра', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: 'token' })))
      .mockResolvedValueOnce(new Response('unavailable', { status: 503 }));
    const f = fixture(fetch);
    const voice = new Voice({ provider: f.provider });
    const speaking = vi.fn();
    voice.setSink({ speaking, visemes: vi.fn(), accent: vi.fn() });
    await expect(voice.say('Привет')).rejects.toThrow('Голос временно недоступен');
    expect(voice.isSpeaking).toBe(false);
    expect(speaking).toHaveBeenLastCalledWith(false);
    expect(f.context.close).toHaveBeenCalledOnce();
  });

  it('таймаут прекращает запрос, контекст и состояние речи', async () => {
    vi.useFakeTimers();
    const f = fixture(vi.fn(() => new Promise<Response>(() => {})), 100);
    const check = expect(f.provider.speak('Привет', f.handlers)).rejects.toThrow('время ожидания');
    await vi.advanceTimersByTimeAsync(101);
    await check;
    expect(f.context.close).toHaveBeenCalledOnce();
    expect(f.handlers.onError).toHaveBeenCalledOnce();
  });

  it('битое аудио и неверный Content-Type не запускают воспроизведение', async () => {
    for (const type of ['text/html', 'audio/wav']) {
      const fetch = vi.fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: 'token' })))
        .mockResolvedValueOnce(new Response('bad', { headers: { 'Content-Type': type } }));
      const f = fixture(fetch);
      f.context.decodeAudioData.mockRejectedValueOnce(new Error('decode'));
      await expect(f.provider.speak('Привет', f.handlers)).rejects.toThrow();
      expect(f.source.start).not.toHaveBeenCalled();
      expect(f.context.close).toHaveBeenCalledOnce();
    }
  });
});
