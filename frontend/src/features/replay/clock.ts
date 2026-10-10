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

  /**
   * Дошли до конца завершённой записи — пауза на конце. Пока запись идёт,
   * часы не встают: позиция держится у края и идёт дальше с новыми данными.
   */
  tick(now: number, duration: number, complete = true) {
    if (!this.playing || this.position(now, duration) < duration) return;
    this.base = duration;
    // Живая запись: «стоим» у края, чтобы новые данные проигрывались, а не появлялись скачком.
    this.startedAt = complete ? null : now;
  }

  /** `complete=false` — запись идёт: с конца не прыгаем в начало, ждём новых данных. */
  play(now: number, duration: number, complete = true) {
    if (this.playing) return;
    // Из конца завершённой записи — с начала, как у обычного проигрывателя.
    if (complete && this.base >= duration) this.base = 0;
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
