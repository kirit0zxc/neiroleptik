/** Встроенные состояния. Словарь перенесён из Yumi 1.0 и расширен. */
export type BuiltinState =
  | 'idle' | 'welcome' | 'curious' | 'thinking' | 'searching'
  | 'analyzing' | 'comparing' | 'excited' | 'success'
  | 'gentle' | 'error' | 'retry' | 'speaking' | 'dance';

/**
 * Имя состояния: встроенное или своё — из студии (presets.json)
 * или defineState(). `string & {}` сохраняет подсказки для встроенных.
 */
export type MascotState = BuiltinState | (string & {});

/** Пресеты выражений VRM 1.0. */
export type VrmExpression =
  | 'neutral' | 'happy' | 'angry' | 'sad' | 'relaxed' | 'surprised';

/** Настройка свечения силуэта (rim light) под состояние. */
export interface GlowSpec {
  /** CSS-цвет свечения. */
  color: string;
  /** 0..1 — базовая сила. */
  intensity: number;
  /** Частота пульсации в герцах; 0 — ровное свечение. */
  pulseHz: number;
}

/** Описание одного состояния: мимика + жест + свет. */
export interface StateSpec {
  expression: VrmExpression;
  /** 0..1 — насколько сильно выкручен блендшейп. */
  weight: number;
  /**
   * Имя клипа из реестра; необязательно. Список — каждый раз
   * случайный клип из него, без повтора предыдущего.
   */
  action?: string | readonly string[];
  /** Зацикливать клип или проиграть один раз. */
  loop?: boolean;
  /**
   * Когда вернуться в idle:
   *   число  — через столько мс
   *   'clip' — когда клип доиграет, сколько бы он ни длился
   *   null   — остаться, пока не придёт другое состояние
   */
  resetAfterMs?: number | 'clip' | null;
  /**
   * Говорить поверх, не прерывая клип: без перехода в speaking.
   * Для длинных клипов вроде танца, которые реплика не должна сбивать.
   */
  speakOver?: boolean;
  /**
   * Двигаться с места, как в клипе: приседания, прыжки, шаги к камере.
   * Камера при этом стоит — Юми может уйти из кадра и вернуться в него.
   * Не задано — как указано у клипа в реестре.
   */
  rootMotion?: boolean;
  /**
   * Следить за курсором головой и глазами. По умолчанию — нет: во время
   * жестов и речи Юми смотрит в камеру. У idle включено.
   */
  followPointer?: boolean;
  /**
   * За сколько мс клип состояния вливается в предыдущий. Больше —
   * плавнее, но вялее. Не задано — 280 мс.
   */
  fadeMs?: number;
  /**
   * Обрезка: играть только [начало, конец] клипа, в секундах исходного
   * клипа. Метки faceTrack тоже в секундах исходного клипа.
   */
  trim?: [number, number];
  /**
   * Дорожка выражений: смена лица по ходу клипа. До первой метки
   * и после конца клипа действует expression/weight. Секунды — от
   * начала клипа; у зацикленного клипа дорожка повторяется с ним.
   */
  faceTrack?: FaceKey[];
  glow: GlowSpec;
}

// Расширение явно: types.ts попадает и в vite.config.ts (nodenext).
import type { ClipMask } from './clips.ts';

/** Метка на дорожке выражений: с момента at (секунды клипа) — такое лицо. */
export interface FaceKey {
  at: number;
  expression: VrmExpression;
  /** 0..1 */
  weight: number;
}

/** Контракт рендерера. Ядро знает только это и ничего про three.js. */
export interface MascotRenderer {
  init(container: HTMLElement): Promise<void>;
  setExpression(name: VrmExpression, weight: number): void;
  /** Куда смотреть: -1..1 по обеим осям, (0,0) — прямо в камеру. */
  setGaze(x: number, y: number): void;
  /** Раскрытие рта 0..1 — совместимость, мапится на визему «аа». */
  setMouth(openness: number): void;
  /** Веса визем aa/ih/ou/ee/oh — сюда пишет липсинк. */
  setVisemes(weights: Record<string, number>): void;
  /** Речь идёт или нет: на время речи эмоция приглушается. */
  setSpeaking(active: boolean): void;
  /** Короткий акцент речи. */
  accent(kind: 'nod' | 'question' | 'pause' | 'emphasis'): void;
  blink(durationMs?: number): void;
  /**
   * mask — переопределить маску клипа из реестра: например, 'none',
   * чтобы корпус двигался как в клипе (приседания, прыжки, шаги).
   */
  playClip(name: string, opts?: {
    loop?: boolean; fadeMs?: number; mask?: ClipMask;
    /** Играть только отрезок [начало, конец] клипа, секунды. */
    range?: readonly [number, number];
    /** Куда вернуться, когда разовый клип доиграет (по умолчанию — последняя петля). */
    returnTo?: string;
  }): void;
  /**
   * Дорожка выражений для клипа: пока играет clip, лицо берётся
   * из меток по времени клипа. null — дорожки нет.
   */
  setFaceTrack?(clip: string | null, keys: readonly FaceKey[] | null): void;
  /**
   * Сюда рендерер сообщает, что разовый клип доиграл. Ставит ядро;
   * без поддержки состояния с resetAfterMs: 'clip' уходят по запасному таймеру.
   */
  onClipEnd?: ((name: string) => void) | null;
  stopClip(fadeMs?: number): void;
  setGlow(spec: GlowSpec): void;
  /**
   * Где на экране голова, в координатах окна. Нужна, чтобы взгляд
   * считался от самой Юми. Нет метода — берётся центр контейнера.
   */
  getHeadScreenPosition?(): { x: number; y: number } | null;
  /** Что этот рендерер умеет — ядро сверяется перед вызовом. */
  capabilities(): { clips: string[]; expressions: string[] };
  dispose(): void;
}

/** Реакция на событие пайплайна. */
export interface Reaction {
  state: MascotState;
  /** Реплика в пузырь; необязательна. */
  say?: string;
}

export type MascotEvent =
  | { type: 'state'; state: MascotState }
  | { type: 'say'; text: string }
  | { type: 'speaking'; active: boolean }
  | { type: 'muted'; muted: boolean }
  | { type: 'clip'; name: string }
  | { type: 'ready' }
  /** Рендерер отсоединён: картинки нет, пока не подключат новый. */
  | { type: 'detached' }
  | { type: 'error'; error: unknown };

export type Listener = (e: MascotEvent) => void;
