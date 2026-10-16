import { sanitizeTerminal } from '../../shared/security/terminal';
import type { MascotSignal } from '../../shared/mascot-slot/mascotEvents';

/**
 * Память реакций Юми на ленту (specs/frontend.md, раздел 9,
 * «Нарастание и акценты»). События ленты проходят через неё *до*
 * схлопывания в {@link Yumi.attack} — иначе шторм не досчитать. Память
 * обогащает payload, а таблица правил ({@link YUMI_RULES}) решает, что
 * сказать. Состояние живёт на инстансе Юми и обнуляется вместе с ней.
 */

/** Окно подсчёта однотипных событий с одного адреса, мс. */
export const STORM_WINDOW_MS = 30_000;
/** Со скольких однотипных событий Юми называет это штормом/перебором. */
export const STORM_THRESHOLD = 5;
/** Верхние границы памяти даже при непрерывном потоке новых адресов. */
export const MAX_REACTION_KEYS = 1024;
export const MAX_REACTION_HITS = 256;

/** Поля события атаки, которые читают реплики (строит `attackPayload`). */
export interface AttackPayload {
  ip: string;
  protocol: string;
  username: string;
  success: boolean;
  command: string;
  truncated: boolean;
  method: string;
  bytes: number | null;
}

/** Что память добавляет к payload для правил. */
export interface Enrichment {
  /** Однотипных событий с адреса за окно (≥1); для входа — только неудачи. */
  repeat: number;
  /** Индекс варианта обычной реплики: 0 — исходная из таблицы. */
  variant: number;
  /** Первый успешный вход атакующего за сессию. */
  first: boolean;
  /** Команда из списка опасных. */
  danger: boolean;
  /** Ровно порог перебора — разовая тревога в обход схлопывания. */
  milestone: boolean;
}

/**
 * Опасные команды атакующего — тревожный тон. Управляющие символы уже
 * сняты (S2), поэтому ищем по чистому тексту.
 */
const DANGER_PATTERNS: readonly RegExp[] = [
  /\bmkfs\b/i,
  /\bdd\s+if=/i,
  /:\s*\(\s*\)\s*\{/, // форк-бомба :(){ :|:& };:
  /\bchmod\s+(-\S+\s+)?777\b/i,
  /\/etc\/(passwd|shadow)\b/i,
  /\b(wget|curl)\b[^\n|]*\|\s*(sh|bash|zsh)\b/i, // скачать и выполнить
  />\s*\/dev\/sd[a-z]/i, // запись в диск
];

/** Опасна ли команда атакующего (после снятия управляющих символов). */
export function isDangerousCommand(command: string): boolean {
  const clean = sanitizeTerminal(command);
  // Собираем только соседние короткие опции перед первым операндом/--.
  // Флаги могут быть разделены пробелами, но не другой shell-командой.
  const removals = clean.matchAll(/\brm[ \t]+((?:-[a-zA-Z]+(?:[ \t]+|(?=$|[;|&\n])))+)/g);
  for (const match of removals) {
    const flags = match[1] ?? '';
    if (/[rR]/.test(flags) && /f/.test(flags)) return true;
  }
  return DANGER_PATTERNS.some((re) => re.test(clean));
}

/** Какие сигналы копятся по адресу для «нарастания». */
function countKeyOf(signal: MascotSignal, payload: AttackPayload): string | null {
  const ip = payload.ip;
  if (!ip) return null;
  // Вход считаем штормом только по неудачным попыткам (перебор).
  if (signal === 'attack:auth') return payload.success ? null : `attack:auth:${ip}`;
  if (signal === 'attack:connect') return `attack:connect:${ip}`;
  return null;
}

export class ReactionMemory {
  /** key (сигнал+адрес) → метки времени событий в окне. */
  private readonly hits = new Map<string, number[]>();
  /** Сигнал → счётчик для чередования вариантов реплик. */
  private readonly variants = new Map<string, number>();
  /** Был ли уже успешный вход атакующего за сессию. */
  private sawLogin = false;

  /** Посчитать событие и вернуть поля для правил. `now` — для тестов. */
  enrich(signal: MascotSignal, payload: AttackPayload, now: number = Date.now()): Enrichment {
    this.prune(now);
    const repeat = this.count(signal, payload, now);
    const success = payload.success === true;
    const first = signal === 'attack:auth' && success && !this.sawLogin;
    if (first) this.sawLogin = true;
    return {
      repeat,
      variant: this.nextVariant(signal),
      first,
      danger: signal === 'attack:command' && isDangerousCommand(payload.command),
      // Порог перебора объявляем один раз — ровно при его достижении.
      milestone: signal === 'attack:auth' && !success && repeat === STORM_THRESHOLD,
    };
  }

  private count(signal: MascotSignal, payload: AttackPayload, now: number): number {
    const key = countKeyOf(signal, payload);
    if (!key) return 1;
    const win = (this.hits.get(key) ?? []).filter((t) => now - t < STORM_WINDOW_MS);
    if (win.length >= MAX_REACTION_HITS) win.shift();
    win.push(now);
    // Порядок Map отражает последнее обновление: вытесняем наименее активную пару.
    this.hits.delete(key);
    if (this.hits.size >= MAX_REACTION_KEYS) {
      const oldest = this.hits.keys().next().value;
      if (oldest !== undefined) this.hits.delete(oldest);
    }
    this.hits.set(key, win);
    return win.length;
  }

  /** Чистим также адреса, которые больше никогда не пришлют событие. */
  private prune(now: number): void {
    for (const [key, times] of this.hits) {
      const last = times[times.length - 1];
      if (last === undefined || now - last >= STORM_WINDOW_MS) this.hits.delete(key);
    }
  }

  private nextVariant(signal: MascotSignal): number {
    const n = this.variants.get(signal) ?? 0;
    this.variants.set(signal, n + 1);
    return n;
  }
}
