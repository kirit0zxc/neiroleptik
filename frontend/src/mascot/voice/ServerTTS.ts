import { fetchRaw } from '../assets';
import type { TTSHandlers, TTSProvider } from './types';

interface Playback {
  controller: AbortController;
  context: AudioContext;
  source?: AudioBufferSourceNode;
  timer: ReturnType<typeof setTimeout>;
  finish(error?: unknown): void;
}

export interface ServerTTSOptions {
  endpoint?: string;
  timeoutMs?: number;
  /** Подмена транспорта/звука в тестах и средах с собственным fetch. */
  fetch?: typeof fetch;
  createAudioContext?: () => AudioContext;
}

/** WAV с Django, отмена устаревших реплик и липсинк по звуку. */
export class ServerTTS implements TTSProvider {
  readonly name = 'server-piper';
  readonly hasBoundaries = false;
  private current: Playback | null = null;
  private rate = 1;
  private readonly endpoint: string;
  private readonly timeout: number;
  private readonly transport: typeof fetch;
  private readonly createContext: () => AudioContext;

  constructor(options: ServerTTSOptions = {}) {
    const endpoint = options.endpoint ?? '/api/tts/';
    this.endpoint = endpoint.endsWith('/') ? endpoint : endpoint + '/';
    this.timeout = options.timeoutMs ?? 60_000;
    this.transport = options.fetch ?? fetchRaw;
    this.createContext = options.createAudioContext ?? (() => new AudioContext());
  }

  /** Piper управляет длительностью, но не высотой тона или голосами браузера. */
  setOptions(options: { rate?: number }) {
    if (options.rate != null && Number.isFinite(options.rate)) {
      this.rate = Math.min(2, Math.max(0.5, options.rate));
    }
  }

  speak(text: string, handlers: TTSHandlers): Promise<void> {
    this.cancel();
    const clean = text.trim();
    if (!clean) { handlers.onEnd?.(); return Promise.resolve(); }
    return new Promise<void>((resolve, reject) => {
      let context: AudioContext;
      try {
        // Создаём и возобновляем контекст до первого await: сохраняем
        // пользовательский жест для политики автозапуска браузера.
        context = this.createContext();
      } catch (error) {
        handlers.onError?.(error);
        reject(error);
        return;
      }
      const controller = new AbortController();
      let finished = false;
      const run: Playback = {
        controller, context,
        timer: setTimeout(() => run.finish(new Error('Истекло время ожидания голоса.')), this.timeout),
        finish: (error?: unknown) => {
          if (finished) return;
          finished = true;
          clearTimeout(run.timer);
          controller.abort();
          if (run.source) {
            run.source.onended = null;
            try { run.source.stop(); } catch { /* воспроизведение уже завершено */ }
            run.source.disconnect();
          }
          void context.close().catch(() => {});
          if (this.current === run) this.current = null;
          if (error !== undefined) {
            handlers.onError?.(error);
            reject(error);
          } else {
            handlers.onEnd?.();
            resolve();
          }
        },
      };
      this.current = run;
      const resumed = context.resume();
      // Обработчик ошибки подключается сразу, ещё до ответа HTTP.
      void resumed.catch((error) => run.finish(error));
      void this.play(clean, handlers, run, resumed).catch((error) => run.finish(error));
    });
  }

  cancel() { this.current?.finish(); }

  private async play(text: string, handlers: TTSHandlers, run: Playback, resumed: Promise<void>) {
    const init = { credentials: 'same-origin' as const, cache: 'no-store' as const, signal: run.controller.signal };
    const tokenResponse = await this.transport(this.endpoint + 'csrf/', init);
    if (this.current !== run) return;
    if (!tokenResponse.ok) throw new Error('Не удалось подготовить голос.');
    const token: unknown = await tokenResponse.json();
    if (this.current !== run) return;
    const csrfToken = (token as { csrfToken?: unknown })?.csrfToken;
    if (typeof csrfToken !== 'string' || !csrfToken) throw new Error('Нет CSRF-токена.');
    const response = await this.transport(this.endpoint, {
      ...init, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
      body: JSON.stringify({ text, rate: this.rate }),
    });
    if (this.current !== run) return;
    if (!response.ok || !response.headers.get('Content-Type')?.startsWith('audio/wav')) {
      throw new Error('Голос временно недоступен.');
    }
    const bytes = await response.arrayBuffer();
    if (this.current !== run) return;
    if (bytes.byteLength > 8 * 1024 * 1024) throw new Error('Ответ голоса слишком большой.');
    const audio = await run.context.decodeAudioData(bytes);
    await resumed;
    if (this.current !== run) return;
    clearTimeout(run.timer);
    const source = run.context.createBufferSource();
    run.source = source;
    source.buffer = audio;
    source.connect(run.context.destination);
    source.onended = () => run.finish();
    handlers.onAudio?.(source, run.context);
    source.start();
    handlers.onStart?.();
  }
}
