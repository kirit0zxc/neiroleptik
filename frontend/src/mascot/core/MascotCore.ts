import { STATES, hasState } from './states';
import { transitionFade } from './transitionTable';
import { prefersReducedMotion } from '../motion';
import { Voice, type VoiceOptions } from '../voice/Voice';
import type { TTSProvider } from '../voice/types';
import type {
  Listener, MascotEvent, MascotRenderer, MascotState, StateSpec,
} from './types';

export interface MascotCoreOptions {
  /** С какого состояния начинаем. */
  initialState?: MascotState;
  /** Следить за курсором (в состояниях с followPointer). */
  trackPointer?: boolean;
  /** Диапазон между морганиями, мс. */
  blinkRange?: [number, number];
  /**
   * Меньше фонового движения: без микро-саккад. По умолчанию 'auto' —
   * по системной настройке prefers-reduced-motion.
   */
  reducedMotion?: boolean | 'auto';
  /** Писать переходы в консоль. */
  debug?: boolean;
  /** Настройки голоса; провайдера можно подменить позже. */
  voice?: VoiceOptions;
}

const DEFAULTS: Required<Omit<MascotCoreOptions, 'debug' | 'voice'>> & { debug: boolean } = {
  initialState: 'idle',
  trackPointer: true,
  blinkRange: [2200, 6500],
  reducedMotion: 'auto',
  debug: false,
};

/**
 * Мозг маскота. Ничего не знает про three.js, DOM трогает только
 * ради слушателя курсора. Всю картинку рисует renderer.
 *
 * Состояния и их таймеры живут независимо от картинки: Юми может быть
 * свёрнута, а пайплайн — слать события. Когда картинка вернётся, она
 * покажет актуальное состояние, а не давно прошедшую радость.
 */
export class MascotCore {
  private renderer: MascotRenderer | null = null;
  /** Рендерер, который ещё грузится. */
  private pendingRenderer: MascotRenderer | null = null;
  /** Клип, который играет текущее состояние, и последний выбранный из списка. */
  private stateClip: string | null = null;
  private lastPicked: string | null = null;
  private opts: typeof DEFAULTS;
  private listeners = new Set<Listener>();

  private state: MascotState;
  private resetTimer: ReturnType<typeof setTimeout> | null = null;
  private blinkTimer: ReturnType<typeof setTimeout> | null = null;
  private saccadeTimer: ReturnType<typeof setTimeout> | null = null;

  /** Куда смотрит, -1..1. Складывается из курсора и микро-саккад. */
  private pointer = { x: 0, y: 0 };
  private saccade = { x: 0, y: 0 };

  private disposed = false;
  private container: HTMLElement | null = null;
  private onPointerMove?: (e: PointerEvent) => void;
  /** Окно, где сейчас Юми: курсор и кадры берём у него. */
  private win: Window = window;
  /** Окно, на которое повешен слушатель курсора, — снимать надо с него же. */
  private pointerWin: Window | null = null;
  /** Выбор пользователя «без звука». */
  private muted = false;
  /** Голос заглушён приложением (например, окно закрыто) — выбор пользователя не трогаем. */
  private suppressed = false;
  /** Последняя позиция курсора; пересчитываем раз в кадр, не на каждое событие. */
  private lastPointer: { x: number; y: number } | null = null;
  private gazeFrame = 0;

  private voice: Voice;
  /** Куда вернуться, когда Юми договорит. */
  private stateBeforeSpeech: MascotState | null = null;

  constructor(options: MascotCoreOptions = {}) {
    this.opts = { ...DEFAULTS, ...options };
    this.state = hasState(this.opts.initialState) ? this.opts.initialState : 'idle';

    this.voice = new Voice(options.voice);
    this.voice.setSink({
      visemes: (w) => this.renderer?.setVisemes(w),
      accent: (kind) => this.renderer?.accent(kind),
      speaking: (active) => this.onSpeaking(active),
    });
  }

  /**
   * Речь началась или кончилась.
   *
   * На время реплики тело уходит в speaking, а потом возвращается:
   * в зацикленное состояние (Юми думала — думает дальше) или в покой,
   * если до реплики был разовый жест — повторять его второй раз странно.
   */
  private onSpeaking(active: boolean) {
    this.renderer?.setSpeaking(active);
    this.emit({ type: 'speaking', active });
    if (active) {
      // Состояние, которое нельзя прерывать (танец), продолжается —
      // реплика идёт поверх: рот артикулирует, тело танцует дальше.
      if (STATES[this.state]?.speakOver) return;
      if (this.state !== 'speaking') this.stateBeforeSpeech = this.state;
      this.setState('speaking');
    } else {
      const back = this.stateBeforeSpeech ?? 'idle';
      this.stateBeforeSpeech = null;
      if (this.state !== 'speaking') return;
      const spec = STATES[back];
      const oneShot = spec && spec.action != null && !spec.loop;
      this.setState(spec && !oneShot ? back : 'idle');
    }
  }

  // ─── Подключение рендерера ────────────────────────────────

  async attach(renderer: MascotRenderer, container: HTMLElement) {
    // StrictMode монтирует дважды — старый рендерер надо убрать,
    // иначе на странице окажутся два канваса.
    this.detach();
    // Пока рендерер грузится, команды ему не шлём: состояние
    // применится целиком, когда он будет готов.
    this.pendingRenderer = renderer;
    try {
      await renderer.init(container);
    } catch (error) {
      // Рендерер упал — маскот молчит, но приложение живёт дальше.
      if (this.pendingRenderer === renderer) this.pendingRenderer = null;
      renderer.dispose();
      this.emit({ type: 'error', error });
      return;
    }
    // Пока грузились, нас могли отсоединить или подменить другим рендерером.
    if (this.disposed || this.pendingRenderer !== renderer) { renderer.dispose(); return; }

    this.pendingRenderer = null;
    this.renderer = renderer;
    renderer.onClipEnd = (name) => this.onClipEnd(name);
    this.container = container;
    this.win = container.ownerDocument.defaultView ?? window;
    this.voice.setFrameSource(this.win);
    // Картинку догоняем до текущего состояния, не перезапуская его таймер.
    // Исключение — «по концу клипа»: клип начнётся заново, и ждать надо его.
    if (STATES[this.state]?.resetAfterMs === 'clip') this.armReset(this.state);
    this.showState(this.state);
    this.startLife();
    this.pushGaze();
    this.emit({ type: 'ready' });
  }

  /**
   * Отсоединить рендерер и остановить всё, что работает на картинку.
   * Состояние и его таймер не трогаем — они живут и без картинки.
   *
   * С аргументом — только если подключён именно он: так размонтирование
   * старого компонента не снесёт рендерер, который уже поставил новый.
   */
  detach(renderer?: MascotRenderer) {
    if (renderer && renderer !== this.renderer && renderer !== this.pendingRenderer) {
      renderer.dispose();
      return;
    }
    const had = this.renderer !== null;
    this.stopLife();
    this.container = null;
    this.pendingRenderer?.dispose();
    this.pendingRenderer = null;
    if (this.renderer) this.renderer.onClipEnd = null;
    this.renderer?.dispose();
    this.renderer = null;
    // Окно (например, Picture-in-Picture) могло закрыться — не держим ссылку.
    this.win = window;
    this.voice.setFrameSource(window);
    // Без картинки конца клипа не будет — переводим ожидание на короткое.
    if (had && STATES[this.state]?.resetAfterMs === 'clip') this.armReset(this.state);
    if (had) this.emit({ type: 'detached' });
  }

  // ─── Состояния ────────────────────────────────────────────

  /**
   * Главная точка входа: сюда шлёт события пайплайн.
   *
   * Плавность перехода: opts.fadeMs (разово) → таблица переходов
   * для пары «прошлое→новое» → fadeMs состояния → 280 мс.
   */
  setState(next: MascotState, opts: { fadeMs?: number } = {}) {
    if (!hasState(next)) {
      if (this.opts.debug) console.warn('[mascot] неизвестное состояние:', next);
      return;
    }
    const prev = this.state;
    this.state = next;
    this.armReset(next);
    this.showState(next, opts.fadeMs ?? transitionFade(prev, next));
    this.pushGaze();
    this.emit({ type: 'state', state: next });
  }

  getState() { return this.state; }

  /** Прямой доступ к рендереру — нужен песочнице и отладке. */
  getRenderer() { return this.renderer; }

  /**
   * Разовый жест поверх текущего состояния — например, кивок из правила
   * моста { state, clip }. Считается клипом состояния: если оно уходит
   * в покой «по концу клипа», конец этого жеста его и завершит.
   */
  playGesture(clip: string) {
    const r = this.renderer;
    this.stateClip = clip;
    if (!r) return;
    r.playClip(clip, { loop: false, fadeMs: DEFAULT_FADE_MS, returnTo: this.idleClip() });
    this.emit({ type: 'clip', name: clip });
  }

  /** Таймер возврата в покой — работает и без картинки. */
  private armReset(name: MascotState) {
    if (this.resetTimer) { clearTimeout(this.resetTimer); this.resetTimer = null; }
    const spec = STATES[name];
    if (!spec) return;
    // 'clip': конец клипа сообщит рендерер; таймер — запасной, на случай
    // если клип не загрузился. Без картинки конца клипа не будет вовсе —
    // тогда уходим в покой через пару секунд, как обычный жест.
    const reset = spec.resetAfterMs === 'clip'
      ? (this.renderer ? CLIP_RESET_FALLBACK_MS : HIDDEN_CLIP_RESET_MS)
      : spec.resetAfterMs;
    if (reset == null) return;
    this.resetTimer = setTimeout(() => {
      this.resetTimer = null;
      if (this.state === name) this.setState('idle');
    }, reset);
  }

  /** Показать состояние на картинке, если она есть. */
  private showState(name: MascotState, fadeMs?: number) {
    // Своё состояние могли удалить на лету — тогда показывать нечего.
    const spec: StateSpec | undefined = STATES[name];
    const r = this.renderer;
    if (!r || !spec) return;

    // Во время речи лицо и свет остаются от прежнего состояния: грустная
    // Юми, объясняющая ошибку, не должна становиться нейтральной.
    const before = name === 'speaking' && this.stateBeforeSpeech ? STATES[this.stateBeforeSpeech] : undefined;
    const look = before ?? spec;
    r.setExpression(look.expression, look.weight);
    r.setGlow(look.glow);

    this.stateClip = null;
    const clip = this.pickClip(spec.action);
    r.setFaceTrack?.(clip, spec.faceTrack ?? null);
    const fade = fadeMs ?? spec.fadeMs ?? DEFAULT_FADE_MS;
    if (clip) {
      // Клипа может не оказаться — тогда останется мимика и свет.
      r.playClip(clip, {
        loop: spec.loop ?? false,
        fadeMs: fade,
        ...(spec.rootMotion != null ? { mask: spec.rootMotion ? 'none' : 'inplace' } : {}),
        // Обрезка привязана к одному клипу; у списка (танцы) её нет.
        ...(spec.trim && typeof spec.action === 'string' ? { range: spec.trim } : {}),
        // Разовый жест доигрывает в покой, а не в петлю прошлого состояния.
        ...(spec.loop ? {} : { returnTo: this.idleClip() }),
      });
      this.stateClip = clip;
      this.emit({ type: 'clip', name: clip });
    } else {
      // Состояние без анимации: тело — в покой, иначе продолжился бы
      // клип прошлого состояния (танец под чужую эмоцию).
      const idle = this.idleClip();
      if (idle) r.playClip(idle, { loop: true, fadeMs: fade });
    }
  }

  /** Петля покоя — куда возвращаются разовые жесты. */
  private idleClip(): string | undefined {
    const action = STATES.idle?.action;
    return typeof action === 'string' ? action : undefined;
  }

  /** Клип для состояния; из списка — случайный, но не тот же, что в прошлый раз. */
  private pickClip(action: StateSpec['action']): string | null {
    if (!action) return null;
    if (typeof action === 'string') return action;
    const pool = action.length > 1 ? action.filter((c) => c !== this.lastPicked) : action;
    const clip = pool[Math.floor(Math.random() * pool.length)] ?? null;
    this.lastPicked = clip;
    return clip;
  }

  /** Рендерер доиграл разовый клип. */
  private onClipEnd(clip: string) {
    if (clip !== this.stateClip) return;
    if (STATES[this.state]?.resetAfterMs !== 'clip') return;
    // Танец кончился посреди реплики — договариваем в обычной речи,
    // а после неё вернёмся в покой.
    if (this.voice.isSpeaking) {
      this.stateBeforeSpeech = 'idle';
      this.setState('speaking');
    } else {
      this.setState('idle');
    }
  }

  // ─── Реплики ──────────────────────────────────────────────

  /** Произнести реплику: голос, липсинк, кивки и возврат в прежнее состояние. */
  say(text: string) {
    this.emit({ type: 'say', text });
    // Без звука реплика остаётся текстом в пузыре.
    if (this.muted || this.suppressed) return;
    void this.voice.say(text).catch((error) => this.emit({ type: 'error', error }));
  }

  /** Без звука (выбор пользователя): реплики только текстом; текущая речь обрывается. */
  setMuted(muted: boolean) {
    this.muted = muted;
    if (muted) this.voice.cancel();
    this.emit({ type: 'muted', muted });
  }

  get isMuted() { return this.muted; }

  /**
   * Заглушить голос по решению приложения — например, пока окно Юми
   * закрыто. Выбор пользователя «без звука» при этом не меняется.
   */
  setVoiceSuppressed(suppressed: boolean) {
    this.suppressed = suppressed;
    if (suppressed) this.voice.cancel();
  }

  /** Оборвать речь на полуслове. */
  hush() { this.voice.cancel(); }

  /**
   * Подменить источник голоса — например, на бэкендовый TTS.
   * Лицо и тело об этом не узнают.
   */
  setVoiceProvider(provider: TTSProvider) { this.voice.setProvider(provider); }

  /** Голос, темп и высота тона. */
  setVoiceOptions(next: { voiceName?: string; rate?: number; pitch?: number }) {
    this.voice.setVoiceOptions(next);
  }

  get isSpeaking() { return this.voice.isSpeaking; }

  /** Раскрытие рта 0..1. Сюда пишет липсинк. */
  setMouth(openness: number) {
    this.renderer?.setMouth(clamp01(openness));
  }

  // ─── Жизнь в простое ──────────────────────────────────────

  /** Моргание, саккады, курсор. Повторный запуск ничего не удваивает. */
  private startLife() {
    this.stopLife();
    this.scheduleBlink();
    if (!prefersReducedMotion(this.opts.reducedMotion)) this.scheduleSaccade();
    if (this.opts.trackPointer) this.startPointerTracking();
  }

  private stopLife() {
    if (this.blinkTimer) { clearTimeout(this.blinkTimer); this.blinkTimer = null; }
    if (this.saccadeTimer) { clearTimeout(this.saccadeTimer); this.saccadeTimer = null; }
    if (this.onPointerMove) {
      this.pointerWin?.removeEventListener('pointermove', this.onPointerMove as EventListener);
      this.pointerWin = null;
      this.onPointerMove = undefined;
    }
    if (this.gazeFrame) { this.win.cancelAnimationFrame(this.gazeFrame); this.gazeFrame = 0; }
  }

  private scheduleBlink() {
    if (this.disposed) return;
    const [lo, hi] = this.opts.blinkRange;
    const wait = lo + Math.random() * (hi - lo);
    this.blinkTimer = setTimeout(() => {
      this.renderer?.blink(110);
      // Иногда человек моргает дважды подряд — это заметно оживляет.
      // Второе моргание идёт через тот же таймер, чтобы stopLife снял и его.
      if (Math.random() < 0.18) {
        this.blinkTimer = setTimeout(() => {
          this.renderer?.blink(90);
          this.scheduleBlink();
        }, 190);
        return;
      }
      this.scheduleBlink();
    }, wait);
  }

  /**
   * Микро-саккады: глаз никогда не стоит идеально ровно.
   * Без них взгляд выглядит стеклянным, с ними — живым.
   */
  private scheduleSaccade() {
    if (this.disposed) return;
    const wait = 900 + Math.random() * 2200;
    this.saccadeTimer = setTimeout(() => {
      this.saccade.x = (Math.random() - 0.5) * 0.14;
      this.saccade.y = (Math.random() - 0.5) * 0.10;
      this.pushGaze();
      this.scheduleSaccade();
    }, wait);
  }

  // ─── Взгляд ───────────────────────────────────────────────

  private startPointerTracking() {
    this.onPointerMove = (e: PointerEvent) => {
      this.lastPointer = { x: e.clientX, y: e.clientY };
      // pointermove приходит чаще кадров, а замер положения головы
      // трогает layout — считаем не чаще одного раза за кадр.
      this.gazeFrame ||= this.win.requestAnimationFrame(() => {
        this.gazeFrame = 0;
        this.updatePointerGaze();
      });
    };
    this.pointerWin = this.win;
    this.win.addEventListener('pointermove', this.onPointerMove as EventListener, { passive: true });
  }

  /**
   * Взгляд считается от головы Юми, а не от центра окна: если она
   * сидит в углу, то должна смотреть на курсор, а не мимо него.
   * Масштаб — полэкрана: курсор у края окна даёт полный поворот.
   */
  private updatePointerGaze() {
    const p = this.lastPointer;
    if (!p) return;
    const anchor = this.renderer?.getHeadScreenPosition?.() ?? this.containerCenter();
    const halfW = Math.max(this.win.innerWidth / 2, 1);
    const halfH = Math.max(this.win.innerHeight / 2, 1);
    this.pointer.x = clampRange((p.x - anchor.x) / halfW);
    this.pointer.y = clampRange(-(p.y - anchor.y) / halfH);
    this.pushGaze();
  }

  private containerCenter() {
    const r = this.container?.getBoundingClientRect();
    return r
      ? { x: r.left + r.width / 2, y: r.top + r.height / 2 }
      : { x: this.win.innerWidth / 2, y: this.win.innerHeight / 2 };
  }

  /** Поставить взгляд вручную, если курсор не нужен (trackPointer: false). */
  lookAt(x: number, y: number) {
    this.pointer.x = clampRange(x);
    this.pointer.y = clampRange(y);
    this.pushGaze();
  }

  /**
   * Куда смотреть. За курсором — только в состояниях с followPointer
   * (по умолчанию это покой); в остальных — в камеру, с саккадами.
   * Сглаживание в рендерере делает возврат взгляда плавным. Взгляд,
   * поставленный вручную через lookAt, действует всегда.
   */
  private pushGaze() {
    const follow = !this.opts.trackPointer || STATES[this.state]?.followPointer === true;
    const px = follow ? this.pointer.x : 0;
    const py = follow ? this.pointer.y : 0;
    this.renderer?.setGaze(clampRange(px + this.saccade.x), clampRange(py + this.saccade.y));
  }

  // ─── Шина событий ─────────────────────────────────────────

  /** Возвращает отписку. Тип void — чтобы годилась как cleanup в useEffect. */
  on(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }

  private emit(e: MascotEvent) {
    if (this.opts.debug) console.debug('[mascot]', e);
    for (const fn of this.listeners) {
      // Падение одного слушателя не должно ронять остальных.
      try { fn(e); } catch (err) { console.error('[mascot] слушатель упал', err); }
    }
  }

  // ─── Уборка ───────────────────────────────────────────────

  dispose() {
    this.disposed = true;
    if (this.resetTimer) { clearTimeout(this.resetTimer); this.resetTimer = null; }
    this.detach();
    this.voice.cancel();
    this.listeners.clear();
  }
}

/** Плавность перехода в клип состояния, если у него не задана своя. */
export const DEFAULT_FADE_MS = 280;

/** Сколько ждать конца клипа, прежде чем уйти в покой без него. */
const CLIP_RESET_FALLBACK_MS = 60_000;

/** Без картинки конца клипа не будет — столько длится «невидимый» жест. */
const HIDDEN_CLIP_RESET_MS = 3000;

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
const clampRange = (v: number) => (v < -1 ? -1 : v > 1 ? 1 : v);
