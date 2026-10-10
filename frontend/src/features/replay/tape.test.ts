// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { ReplayTape, type ReplayChunk } from './tape';

const enc = new TextEncoder();
/** Байты → base64, как в data_base64. */
const b64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes));
let seq = 0;
const chunk = (direction: 'in' | 'out', bytes: Uint8Array | string, extra: Partial<ReplayChunk> = {}): ReplayChunk => ({
  seq: seq++, offset_ms: seq * 100, direction, truncated: false,
  data_base64: b64(typeof bytes === 'string' ? enc.encode(bytes) : bytes), ...extra,
});
const textOf = (tape: ReplayTape, direction?: 'in' | 'out') => tape.segments
  .filter((s) => !direction || s.direction === direction)
  .map((s) => ('gap' in s ? '[разрыв]' : s.text)).join('');

describe('Replay: декодирование (F15)', () => {
  it('символ UTF-8, разрезанный между чанками, собирается', () => {
    const bytes = enc.encode('привет');
    const tape = new ReplayTape();
    tape.append([chunk('out', bytes.slice(0, 3)), chunk('out', bytes.slice(3))]);
    tape.finish();
    expect(textOf(tape)).toBe('привет');
  });

  it('у каждого направления свой декодер: чередование не ломает символы', () => {
    const a = enc.encode('ёж');
    const b = enc.encode('юла');
    const tape = new ReplayTape();
    tape.append([
      chunk('in', a.slice(0, 1)), chunk('out', b.slice(0, 1)),
      chunk('in', a.slice(1)), chunk('out', b.slice(1)),
    ]);
    tape.finish();
    expect(textOf(tape, 'in')).toBe('ёж');
    expect(textOf(tape, 'out')).toBe('юла');
  });

  it('truncated: недописанный символ — U+FFFD, отметка разрыва, декодер сброшен', () => {
    const bytes = enc.encode('я');
    const tape = new ReplayTape();
    tape.append([chunk('out', bytes.slice(0, 1), { truncated: true }), chunk('out', 'ok')]);
    tape.finish();
    expect(textOf(tape)).toBe('�[разрыв]ok');
    expect(tape.segments.some((s) => 'gap' in s)).toBe(true);
  });

  it('повтор чанка с уже виденным seq пропускается', () => {
    const tape = new ReplayTape();
    const c = chunk('out', 'one');
    tape.append([c]);
    tape.append([c, chunk('out', 'two')]);
    tape.finish();
    expect(textOf(tape)).toBe('onetwo');
  });

  it('длительность — offset_ms последнего чанка', () => {
    const tape = new ReplayTape();
    tape.append([chunk('out', 'a', { offset_ms: 0 }), chunk('in', 'b', { offset_ms: 4200 })]);
    expect(tape.durationMs).toBe(4200);
  });
});

describe('Replay: безопасность (S2)', () => {
  it('цвет, гиперссылка OSC 8 и смена заголовка удаляются, текст остаётся', () => {
    const tape = new ReplayTape();
    tape.append([chunk('out', '\u001b[31mred\u001b[0m \u001b]8;;http://evil\u0007link\u001b]8;;\u0007 \u001b]0;title\u0007end')]);
    tape.finish();
    expect(textOf(tape)).toBe('red link end');
  });

  it('последовательность, разрезанная между чанками, тоже удаляется', () => {
    const tape = new ReplayTape();
    tape.append([chunk('out', 'a\u001b[3'), chunk('out', '1mb\u001b]8;;http://x'), chunk('out', '\u0007c')]);
    tape.finish();
    expect(textOf(tape)).toBe('abc');
  });

  it('\\r\\n на границе чанков — один перевод строки', () => {
    const tape = new ReplayTape();
    tape.append([chunk('out', 'a\r'), chunk('out', '\nb')]);
    tape.finish();
    expect(textOf(tape)).toBe('a\nb');
  });

  it('незавершённая последовательность не держится бесконечно', () => {
    const tape = new ReplayTape();
    tape.append([chunk('out', `x\u001b]8;;${'y'.repeat(5000)}`), chunk('out', 'z')]);
    tape.finish();
    expect(textOf(tape).endsWith('z')).toBe(true);
    expect(textOf(tape).includes('\u001b')).toBe(false);
  });
});

describe('Replay: Enter атакующего', () => {
  it('\r в конце чанка виден сразу, \n следующего чанка не удваивает строку', () => {
    const tape = new ReplayTape();
    tape.append([chunk('in', 'whoami\r')]);
    expect(textOf(tape)).toBe('whoami\n');
    tape.append([chunk('in', '\nls')]);
    expect(textOf(tape)).toBe('whoami\nls');
  });

  it('эхо Enter в out (\\r\\n) не даёт лишней пустой строки после команды', () => {
    const tape = new ReplayTape();
    // Ввод 'whoami' + Enter, затем эхо центра: CRLF и результат в out.
    tape.append([chunk('in', 'whoami\r'), chunk('out', '\r\nroot\r\n')]);
    tape.finish();
    // Между командой и выводом ровно один перевод строки, не два.
    expect(textOf(tape)).toBe('whoami\nroot\n');
    expect(textOf(tape)).not.toContain('\n\n');
  });

  it('повторный Enter не теряется и не зависит от разбиения на чанки', () => {
    // Два Enter отдельными чанками (одно направление — ввод).
    const split = new ReplayTape();
    split.append([chunk('in', '\r'), chunk('in', '\r')]);
    split.finish();
    // Одним чанком.
    seq = 0;
    const together = new ReplayTape();
    together.append([chunk('in', '\r\r')]);
    together.finish();
    // Оба нажатия сохранены, и разбиение на чанки не влияет на результат.
    expect(textOf(split)).toBe('\n\n');
    expect(textOf(split)).toBe(textOf(together));
  });

  it('эхо CRLF в out, разрезанное между чанками, даёт один перевод (как слитное)', () => {
    // Enter в in, затем эхо центра CRLF, разрезанное на два out-чанка.
    seq = 0;
    const split = new ReplayTape();
    split.append([chunk('in', 'cmd\r'), chunk('out', '\r'), chunk('out', '\nroot')]);
    split.finish();
    // То же эхо одним чанком.
    seq = 0;
    const together = new ReplayTape();
    together.append([chunk('in', 'cmd\r'), chunk('out', '\r\nroot')]);
    together.finish();
    expect(textOf(split)).toBe('cmd\nroot');
    expect(textOf(split)).toBe(textOf(together));
  });
});
