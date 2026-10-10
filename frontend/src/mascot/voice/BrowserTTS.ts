import type { TTSHandlers, TTSProvider } from './types';

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

export interface VoiceInfo {
  name: string;
  lang: string;
  local: boolean;
  isDefault: boolean;
}

/**
 * Голос средствами браузера. Бесплатно, офлайн, работает сразу.
 *
 * Ограничение, которое определяет всю конструкцию липсинка: Chrome
 * проигрывает синтезированную речь мимо Web Audio, поэтому звук
 * нельзя проанализировать. Зато приходят события границ слов.
 */
/**
 * Голоса, которые берём в первую очередь, по убыванию — подстрока имени.
 * Ирина (Microsoft Irina, Windows) — выбранный голос Юми. Её нет на Mac,
 * Linux и телефонах; там подбор идёт по качеству среди русских голосов.
 */
export const DEFAULT_PREFERRED_VOICES: readonly string[] = ['Irina'];

type BrowserTTSOptions = {
  lang?: string;
  rate?: number;
  pitch?: number;
  /** Точное имя голоса — важнее всех предпочтений. */
  voiceName?: string;
  /** Подстроки имён голосов по приоритету; по умолчанию — Ирина. */
  prefer?: readonly string[];
};

/** Минимум, по которому выбирается голос: есть и у системного голоса, и у VoiceInfo. */
type VoiceLike = { name: string; lang: string; default?: boolean };

export class BrowserTTS implements TTSProvider {
  readonly name = 'browser';
  readonly hasBoundaries = true;

  private opts: BrowserTTSOptions;
  private lastCancelAt = 0;
  /** Растёт на каждой отмене: реплика, ждавшая паузы, понимает, что её отменили. */
  private generation = 0;
  /** Завершить текущую реплику: снять её таймеры и отпустить промис. */
  private finishCurrent: (() => void) | null = null;

  constructor(opts: BrowserTTSOptions = {}) {
    this.opts = opts;
  }

  static isAvailable() {
    return typeof window !== 'undefined' && 'speechSynthesis' in window;
  }

  /**
   * Список голосов приходит асинхронно.
   *
   * При первом обращении getVoices() почти всегда пуст, и если
   * спросить его синхронно, окажется что голосов «нет» — хотя через
   * мгновение они появятся. Это самая частая причина немой первой фразы.
   */
  static async loadVoices(timeoutMs = 1500): Promise<SpeechSynthesisVoice[]> {
    if (!BrowserTTS.isAvailable()) return [];
    const have = speechSynthesis.getVoices();
    if (have.length) return have;

    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        resolve(speechSynthesis.getVoices());
      };
      speechSynthesis.addEventListener('voiceschanged', finish, { once: true });
      setTimeout(finish, timeoutMs);
    });
  }

  /** Что доступно в системе — для диагностики. */
  static async list(): Promise<VoiceInfo[]> {
    const voices = await BrowserTTS.loadVoices();
    return voices.map((v) => ({
      name: v.name, lang: v.lang, local: v.localService, isDefault: v.default,
    }));
  }

  /** Поменять голос и тембр на лету. */
  setOptions(next: Partial<typeof this.opts>) {
    this.opts = { ...this.opts, ...next };
  }

  getOptions() { return { ...this.opts }; }

  /**
   * Насколько голос подходит Юми.
   *
   * Сначала — предпочтённые (по умолчанию Ирина). Дальше — по качеству:
   * нейросетевые голоса (Google, «Natural»/«Neural») живее системных
   * SAPI, которые узнаются по «роботности» с первых слов.
   */
  private static score(v: VoiceLike, prefer: readonly string[]) {
    const n = v.name.toLowerCase();
    const preferred = prefer.findIndex((p) => n.includes(p.toLowerCase()));
    if (preferred >= 0) return 1000 - preferred * 10;
    let s = 0;
    if (n.includes('google')) s += 100;
    if (n.includes('natural') || n.includes('neural')) s += 80;
    if (n.includes('online')) s += 30;
    if (n.includes('microsoft') && !n.includes('natural')) s -= 25;
    if (v.default) s += 5;
    return s;
  }

  /**
   * Выбрать голос из списка: точное имя → предпочтённые → лучший на
   * нужном языке → любой. Им же пользуется песочница, чтобы показать
   * в списке тот голос, которым Юми и заговорит.
   */
  static choose<V extends VoiceLike>(
    voices: readonly V[],
    { lang = 'ru-RU', voiceName, prefer = DEFAULT_PREFERRED_VOICES }: BrowserTTSOptions = {},
  ): V | null {
    if (voiceName) {
      const exact = voices.find((v) => v.name === voiceName);
      if (exact) return exact;
    }
    const code = lang.slice(0, 2);
    const byLang = voices.filter((v) => v.lang.replace('_', '-').slice(0, 2) === code);
    if (byLang.length) {
      return [...byLang].sort((a, b) => BrowserTTS.score(b, prefer) - BrowserTTS.score(a, prefer))[0] ?? null;
    }
    // Русского голоса нет — лучше прочитать чужим, чем промолчать.
    return voices.find((v) => v.default) ?? voices[0] ?? null;
  }

  private pick(voices: SpeechSynthesisVoice[], lang: string) {
    return BrowserTTS.choose(voices, { ...this.opts, lang });
  }

  async speak(text: string, handlers: TTSHandlers): Promise<void> {
    const gen = ++this.generation;
    // Пока мы ждём голоса и паузу, нас могли отменить или вытеснить.
    const stale = () => gen !== this.generation;
    if (!BrowserTTS.isAvailable()) {
      handlers.onError?.(new Error('speechSynthesis недоступен в этом браузере'));
      handlers.onEnd?.();
      return;
    }

    const lang = this.opts.lang ?? 'ru-RU';
    const voices = await BrowserTTS.loadVoices();
    if (stale()) { handlers.onEnd?.(); return; }
    if (!voices.length) {
      handlers.onError?.(new Error('в системе не найдено ни одного голоса'));
      handlers.onEnd?.();
      return;
    }

    // Chrome иногда застревает в паузе после предыдущей отмены
    // и молча не проигрывает ничего нового.
    if (speechSynthesis.paused) speechSynthesis.resume();

    // cancel() сразу перед speak() в Chrome нередко съедает новую
    // реплику — отменяем только если и правда что-то идёт, и даём вдохнуть.
    if (speechSynthesis.speaking || speechSynthesis.pending) {
      this.finishCurrent?.();
      speechSynthesis.cancel();
      this.lastCancelAt = performance.now();
      await delay(120);
    }
    // Отмена могла прийти и снаружи — выдерживаем паузу и тогда.
    const sinceCancel = performance.now() - this.lastCancelAt;
    if (sinceCancel < 120) await delay(120 - sinceCancel);
    if (stale()) { handlers.onEnd?.(); return; }

    const voice = this.pick(voices, lang);

    return new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = voice?.lang ?? lang;
      u.rate = this.opts.rate ?? 1;
      u.pitch = this.opts.pitch ?? 1.05;
      if (voice) u.voice = voice;

      // Таймеры у каждой реплики свои: общие поля перетирались бы
      // следующей репликой, и конец старой снимал бы сторожа новой.
      let watchdog: ReturnType<typeof setTimeout> | undefined;
      let keepAlive: ReturnType<typeof setInterval> | undefined;
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        clearTimeout(watchdog);
        clearInterval(keepAlive);
        if (this.finishCurrent === finish) this.finishCurrent = null;
        handlers.onEnd?.();
        resolve();
      };
      this.finishCurrent = finish;

      u.onstart = () => handlers.onStart?.();
      u.onboundary = (e) => {
        if (e.name === 'word' || e.name === undefined) handlers.onBoundary?.(e.charIndex);
      };
      u.onend = finish;
      u.onerror = (e) => {
        const err = (e as SpeechSynthesisErrorEvent).error;
        // Отмена — не ошибка: просто следующая реплика вытеснила эту.
        if (err !== 'canceled' && err !== 'interrupted') handlers.onError?.(e);
        finish();
      };

      // Chrome умеет не прислать onend вовсе. Без сторожа рот
      // остался бы открытым, а состояние — залипшим в speaking.
      const estimate = 1200 + text.length * 110 / (u.rate || 1);
      watchdog = setTimeout(() => {
        if (!speechSynthesis.speaking) finish();
      }, estimate * 1.8);

      speechSynthesis.speak(u);

      // Chrome сам уходит в паузу примерно через 15 секунд речи
      // и замолкает на середине. Периодический pause/resume держит
      // движок в тонусе — известный обходной приём.
      keepAlive = setInterval(() => {
        if (!speechSynthesis.speaking) return;
        speechSynthesis.pause();
        speechSynthesis.resume();
      }, 9000);
    });
  }

  cancel() {
    this.generation++;
    this.finishCurrent?.();
    if (BrowserTTS.isAvailable()) {
      speechSynthesis.cancel();
      this.lastCancelAt = performance.now();
    }
  }
}
