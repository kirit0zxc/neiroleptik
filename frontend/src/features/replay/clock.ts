/** Скорости проигрывателя (specs, «Replay», проигрыватель). */
export const SPEEDS = [0.5, 1, 2, 4, 8] as const;
export type Speed = (typeof SPEEDS)[number];

/**
 * Часы проигрывателя: позиция в мс записи, пуск/пауза, скорость, перемотка.
 * Время передаётся снаружи (`now`) — часы детерминированы в тестах.
 * Позиция не выходит за длительность; дойдя до конца, часы встают на паузу.
 */
export class ReplayClock {
  private base = 0;
  /** Момент пуска по `now`; null — пауза. */
  private startedAt: number | null = null;
  speed: Speed = 1;

  get playing() { return this.startedAt !== null; }

  position(now: number, duration: number): number {
    const raw = this.startedAt === null ? this.base : this.base + (now - this.startedAt) * this.speed;
    return Math.min(Math.max(raw, 0), duration);
  }

  /** Дошли до конца — пауза на конце (при живой записи пуск продолжит с этого места). */
  tick(now: number, duration: number) {
    if (this.playing && this.position(now, duration) >= duration) {
      this.base = duration;
      this.startedAt = null;
    }
  }

  play(now: number, duration: number) {
    if (this.playing) return;
    // Из конца — с начала, как у обычного проигрывателя.
    if (this.base >= duration) this.base = 0;
    this.startedAt = now;
  }

  pause(now: number, duration: number) {
    this.base = this.position(now, duration);
    this.startedAt = null;
  }

  seek(ms: number, now: number, duration: number) {
    this.base = Math.min(Math.max(ms, 0), duration);
    if (this.playing) this.startedAt = now;
  }

  setSpeed(speed: Speed, now: number, duration: number) {
    this.base = this.position(now, duration);
    if (this.playing) this.startedAt = now;
    this.speed = speed;
  }
}

/** «мм:сс» от начала сессии; часы больше 99 минут не обрезаются. */
export function formatOffset(ms: number): string {
  const total = Math.floor(Math.max(ms, 0) / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
