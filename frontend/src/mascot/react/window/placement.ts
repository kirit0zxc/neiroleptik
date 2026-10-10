/** Режим окна: открыто на странице, свёрнуто во вкладку, закрыто. */
export type WindowMode = 'open' | 'minimized' | 'closed';

export type WindowFit = 'full' | 'half' | 'portrait';

export interface Placement {
  /** Желаемое положение. На экране окно может стоять ближе к краю, если экран уже. */
  x: number;
  y: number;
  mode: WindowMode;
  muted: boolean;
  /** Кадр, выбранный в меню «Вид»; не задан — кадр по умолчанию. */
  fit?: WindowFit;
}

export interface Size { width: number; height: number }

/** Отступ от краёв экрана, в который окно не заезжает. */
const EDGE = 8;

/**
 * Где стоит окно Юми и в каком оно режиме.
 *
 * Положение запоминается в localStorage — это удобство конкретного
 * пользователя, а не данные: если хранилище недоступно (приватный
 * режим, запрет сайта), окно просто открывается на месте по умолчанию.
 */
export class WindowPlacement {
  private readonly key: string;
  private readonly storage: Storage | null;

  constructor(key = 'yumi.window.v1', storage: Storage | null = safeStorage()) {
    this.key = key;
    this.storage = storage;
  }

  /**
   * Положение по умолчанию: слева внизу. На узком экране окно сразу
   * свёрнуто — там оно закрыло бы половину интерфейса.
   */
  initial(viewport: Size, window: Size): Placement {
    return {
      x: EDGE * 2,
      y: Math.max(EDGE, viewport.height - window.height - EDGE * 3),
      mode: viewport.width < 600 ? 'minimized' : 'open',
      muted: false,
    };
  }

  load(viewport: Size, window: Size): Placement {
    const fallback = this.initial(viewport, window);
    try {
      const raw = JSON.parse(this.storage?.getItem(this.key) ?? 'null');
      if (!raw || typeof raw !== 'object') return fallback;
      const mode: WindowMode = ['open', 'minimized', 'closed'].includes(raw.mode) ? raw.mode : fallback.mode;
      // Положение не зажимаем: это желаемое место. Зажимает отрисовка —
      // так временно узкий экран не перезатирает выбор пользователя.
      return {
        x: Number.isFinite(raw.x) ? raw.x : fallback.x,
        y: Number.isFinite(raw.y) ? raw.y : fallback.y,
        mode,
        muted: raw.muted === true,
        ...(['full', 'half', 'portrait'].includes(raw.fit) ? { fit: raw.fit as WindowFit } : {}),
      };
    } catch {
      return fallback;
    }
  }

  save(p: Placement) {
    try { this.storage?.setItem(this.key, JSON.stringify(p)); } catch { /* хранилище недоступно */ }
  }

  /** Окно целиком в экране: заголовок всегда можно схватить. */
  clamp(p: { x: number; y: number }, viewport: Size, window: Size): { x: number; y: number } {
    const maxX = Math.max(EDGE, viewport.width - window.width - EDGE);
    const maxY = Math.max(EDGE, viewport.height - window.height - EDGE);
    return {
      x: Math.round(Math.min(Math.max(p.x, EDGE), maxX)),
      y: Math.round(Math.min(Math.max(p.y, EDGE), maxY)),
    };
  }
}

function safeStorage(): Storage | null {
  try { return globalThis.localStorage ?? null; } catch { return null; }
}
