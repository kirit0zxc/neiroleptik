import { AudioLipSync } from './AudioLipSync';
import { BrowserTTS } from './BrowserTTS';
import { planSpeech, type SpeechPlan } from './ruVisemes';
import type { SpeechAccent, TTSProvider, VisemeWeights } from './types';

export interface VoiceSink {
  /** Текущие веса визем, каждый кадр речи. */
  visemes(weights: VisemeWeights): void;
  /** Короткий акцент: кивок, брови на вопросе, пауза. */
  accent(kind: SpeechAccent['kind']): void;
  /** Речь началась или закончилась. */
  speaking(active: boolean): void;
}

export interface VoiceOptions {
  provider?: TTSProvider;
  rate?: number;
  /** Насколько широко открывается рот, 0..1. */
  mouthGain?: number;
}

/**
 * Голос и мимика речи.
 *
 * Раскладка рта считается из текста заранее, а события границ слов
 * от синтезатора подтягивают её к реальному произношению. Если
 * провайдер отдаёт звук (бэкендовый TTS), включается анализ
 * амплитуды и спектра и вытесняет текстовую раскладку.
 */
export class Voice {
  private provider: TTSProvider;
  private audio = new AudioLipSync();
  private sink: VoiceSink | null = null;

  private plan: SpeechPlan | null = null;
  private startedAt = 0;
  /** Сдвиг раскладки, накопленный из событий границ слов. */
  private driftMs = 0;
  private firedAccents = new Set<number>();
  /**
   * Номер текущей реплики. Колбэки провайдера от вытесненной реплики
   * приходят с опозданием, и без этой сверки onEnd старой фразы
   * закрыл бы рот новой.
   */
  private seq = 0;
  private frame = 0;
  private win: Window = window;
  private rate: number;
  private gain: number;

  constructor(opts: VoiceOptions = {}) {
    this.provider = opts.provider ?? new BrowserTTS({ rate: opts.rate ?? 1 });
    this.rate = opts.rate ?? 1;
    this.gain = opts.mouthGain ?? 0.9;
  }

  setSink(sink: VoiceSink) { this.sink = sink; }

  /**
   * Чьими кадрами считать липсинк. Юми в отдельном окне говорит, пока
   * главная вкладка скрыта, — кадры главного окна в это время стоят.
   */
  setFrameSource(win: Window) {
    if (this.frame) { this.win.cancelAnimationFrame(this.frame); this.frame = 0; }
    this.win = win;
    if (this.plan) this.tick();
  }

  /** Настроить тембр текущего провайдера, если он это умеет. */
  setVoiceOptions(next: { voiceName?: string; rate?: number; pitch?: number }) {
    if (next.rate != null) this.rate = next.rate;
    const p = this.provider as { setOptions?: (o: unknown) => void };
    p.setOptions?.(next);
  }

  /** Подменить источник голоса, не трогая всё остальное. */
  setProvider(provider: TTSProvider) {
    this.provider.cancel();
    this.provider = provider;
  }

  get isSpeaking() { return this.plan !== null; }

  async say(text: string) {
    const clean = text.trim();
    if (!clean) return;

    // Важно: НЕ зовём provider.cancel() здесь. В Chrome отмена
    // вплотную перед speak() глушит новую реплику — гасим только
    // свою раскладку, а передачу эстафеты делает сам провайдер.
    this.stopLocal();
    const id = ++this.seq;
    const mine = () => id === this.seq;
    this.plan = planSpeech(clean, this.rate);
    this.firedAccents.clear();
    this.driftMs = 0;
    this.startedAt = performance.now();
    this.sink?.speaking(true);
    this.tick();

    try {
      await this.provider.speak(clean, {
        onStart: () => { if (mine()) this.startedAt = performance.now(); },
        onBoundary: (charIndex) => { if (mine()) this.resync(charIndex); },
        onAudio: (node, ctx) => { if (mine()) this.audio.attach(node, ctx); },
        onEnd: () => { if (mine()) this.stop(); },
        onError: () => { if (mine()) this.stop(); },
      });
    } catch (error) {
      // Провайдер упал, не позвав ни onEnd, ни onError (сеть бэкендового
      // TTS, битое аудио). Без этого Юми навсегда осталась бы «говорящей».
      if (mine()) this.stop();
      throw error;
    }
  }

  cancel() {
    this.seq++;
    this.provider.cancel();
    this.stop();
  }

  /** Остановить только свою часть: раскладку рта и цикл кадров. */
  private stopLocal() {
    if (this.frame) { this.win.cancelAnimationFrame(this.frame); this.frame = 0; }
    this.plan = null;
    this.audio.detach();
  }

  private stop() {
    const wasActive = this.plan !== null || this.frame !== 0;
    this.stopLocal();
    if (!wasActive) return;
    this.sink?.visemes({});
    this.sink?.speaking(false);
  }

  /**
   * Синтезатор дошёл до слова — подвинуть раскладку туда же.
   *
   * Без этого расхождение копится: оценка длительности по символам
   * всегда немного врёт, и к концу длинной фразы рот уходит вперёд
   * или отстаёт на полслова.
   */
  private resync(charIndex: number) {
    const plan = this.plan;
    if (!plan) return;

    let planned = 0;
    for (const step of plan.steps) {
      if (step.charIndex >= charIndex) break;
      planned += step.durationMs;
    }
    const actual = performance.now() - this.startedAt;
    // Подтягиваем мягко, чтобы рот не дёргался на каждом слове.
    this.driftMs += (planned - actual - this.driftMs) * 0.5;
  }

  private tick = () => {
    const plan = this.plan;
    if (!plan) return;
    this.frame = this.win.requestAnimationFrame(this.tick);

    // Живой звук точнее любой оценки по тексту — если он есть, верим ему.
    if (this.audio.active) {
      const w = this.audio.sample();
      this.sink?.visemes(scale(w, this.gain));
      return;
    }

    const elapsed = performance.now() - this.startedAt + this.driftMs;
    let acc = 0;
    let current = null as (typeof plan.steps)[number] | null;
    for (const step of plan.steps) {
      acc += step.durationMs;
      if (elapsed < acc) { current = step; break; }
    }

    if (!current) {
      // Текст кончился раньше голоса — закрываем рот и ждём onEnd.
      this.sink?.visemes({});
    } else {
      this.sink?.visemes(
        current.viseme ? scale({ [current.viseme]: current.weight }, this.gain) : {},
      );
      this.fireAccents(current.charIndex);
    }
  };

  private fireAccents(charIndex: number) {
    const plan = this.plan;
    if (!plan) return;
    for (const a of plan.accents) {
      if (a.atCharIndex <= charIndex && !this.firedAccents.has(a.atCharIndex)) {
        this.firedAccents.add(a.atCharIndex);
        this.sink?.accent(a.kind);
      }
    }
  }
}

const scale = (w: VisemeWeights, k: number): VisemeWeights => {
  const out: VisemeWeights = {};
  for (const [key, v] of Object.entries(w)) {
    out[key as keyof VisemeWeights] = (v ?? 0) * k;
  }
  return out;
};
