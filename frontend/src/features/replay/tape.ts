import { sanitizeTerminal } from '../../shared/security/terminal';

/** Чанк записи сессии (ReplayChunkV1, contracts/replay-chunk-v1.schema.json). */
export interface ReplayChunk {
  seq: number;
  /** Время от начала сессии, мс. */
  offset_ms: number;
  direction: Direction;
  /** Исходные байты, не HTML. */
  data_base64: string;
  /** Данные потеряны — показываем разрыв. */
  truncated: boolean;
}

export type Direction = 'in' | 'out';

/** Часть ленты: текст направления или отметка разрыва. */
export type Segment =
  | { seq: number; offsetMs: number; direction: Direction; text: string }
  | { seq: number; offsetMs: number; direction: Direction; gap: true };

/** Сколько символов ждать завершения управляющей последовательности. */
const CARRY_MAX = 4096;

// Файл намеренно разбирает управляющие символы терминала.
/* eslint-disable no-control-regex */
/** Завершённые последовательности от ESC: CSI, OSC, DCS/SOS/PM/APC, прочие. */
const COMPLETE = [
  /^\u001b\[[0-?]*[ -/]*[@-~]/,
  /^\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)/,
  /^\u001b[PX^_][\s\S]*?\u001b\\/,
  /^\u001b[@-Z\\-_]/,
];

function base64Bytes(data: string): Uint8Array {
  const raw = atob(data);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

/**
 * Лента записи сессии (specs/frontend.md, «Replay»): чанки → безопасный
 * текст по направлениям. Для каждого направления свой потоковый UTF-8
 * декодер (символ может быть разрезан между чанками); на разрыве
 * (`truncated`) декодер сбрасывается и в ленте появляется отметка.
 * Управляющие последовательности удаляются (S2), а незавершённая в конце
 * чанка ждёт продолжения в следующем чанке того же направления.
 */
export class ReplayTape {
  readonly segments: Segment[] = [];
  private readonly decoders: Record<Direction, TextDecoder> = { in: new TextDecoder('utf-8'), out: new TextDecoder('utf-8') };
  /** Хвост, который нельзя показывать до следующего чанка: начало ESC-последовательности. */
  private readonly carry: Record<Direction, string> = { in: '', out: '' };
  private readonly last: Record<Direction, { seq: number; offsetMs: number } | null> = { in: null, out: null };
  /** Чанк закончился на CR: ведущий LF следующего чанка — та же пара CRLF. */
  private readonly skipLf: Record<Direction, boolean> = { in: false, out: false };
  private seen = new Set<number>();

  /** Длительность записи — время последнего чанка. */
  durationMs = 0;

  /** Добавить страницу чанков (порядок seq ASC); повторы пропускаются. */
  append(chunks: readonly ReplayChunk[]): void {
    for (const c of chunks) {
      if (this.seen.has(c.seq)) continue;
      this.seen.add(c.seq);
      this.durationMs = Math.max(this.durationMs, c.offset_ms);
      this.last[c.direction] = { seq: c.seq, offsetMs: c.offset_ms };
      const decoder = this.decoders[c.direction];
      let text = decoder.decode(base64Bytes(c.data_base64), { stream: !c.truncated });
      if (c.truncated) {
        // Разрыв: недописанный символ уже отдан как U+FFFD (decode без stream),
        // начатая последовательность выбрасывается — её продолжение потеряно.
        this.decoders[c.direction] = new TextDecoder('utf-8');
        this.skipLf[c.direction] = false;
        this.emit(c.direction, c.seq, c.offset_ms, this.carry[c.direction] + text, false);
        this.carry[c.direction] = '';
        this.segments.push({ seq: c.seq, offsetMs: c.offset_ms, direction: c.direction, gap: true });
        continue;
      }
      text = this.carry[c.direction] + text;
      if (this.skipLf[c.direction] && text) {
        if (text.startsWith('\n')) text = text.slice(1);
        this.skipLf[c.direction] = false;
      }
      const cut = this.holdBack(text);
      this.carry[c.direction] = text.slice(cut);
      const shown = text.slice(0, cut);
      // \r показываем сразу (Enter атакующего виден без ожидания следующего чанка).
      if (shown.endsWith('\r')) this.skipLf[c.direction] = true;
      this.emit(c.direction, c.seq, c.offset_ms, shown, true);
    }
  }

  /** Запись завершена: отдать задержанные хвосты. */
  finish(): void {
    for (const d of ['in', 'out'] as const) {
      const rest = this.carry[d] + this.decoders[d].decode();
      this.carry[d] = '';
      const at = this.last[d];
      if (rest && at) this.emit(d, at.seq, at.offsetMs, rest, false);
    }
  }

  /**
   * Где отрезать показываемое: незавершённая ESC-последовательность в конце
   * ждёт продолжения (но не дольше CARRY_MAX).
   */
  private holdBack(text: string): number {
    const esc = text.lastIndexOf('\u001b');
    if (esc >= 0 && text.length - esc <= CARRY_MAX && !COMPLETE.some((re) => re.test(text.slice(esc)))) return esc;
    return text.length;
  }

  private emit(direction: Direction, seq: number, offsetMs: number, raw: string, merge: boolean) {
    const text = sanitizeTerminal(raw);
    if (!text) return;
    // Соседние части одного направления и чанка склеиваем — меньше узлов в ленте.
    const prev = this.segments.at(-1);
    if (merge && prev && !('gap' in prev) && prev.direction === direction && prev.seq === seq) {
      this.segments[this.segments.length - 1] = { ...prev, text: prev.text + text };
      return;
    }
    this.segments.push({ seq, offsetMs, direction, text });
  }
}
