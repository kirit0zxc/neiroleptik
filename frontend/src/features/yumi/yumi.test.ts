// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MascotCore } from '../../mascot/core/MascotCore';
import { Yumi } from './yumi';

describe('Юми в HoneyForge (раздел 9)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('без голоса: ядро в режиме muted, реплики — в лог консоли', () => {
    const core = new MascotCore();
    let muted = false;
    core.on((e) => { if (e.type === 'muted') muted = e.muted; });
    const yumi = new Yumi(core);
    // Звук выключает сама Юми при создании.
    expect(muted).toBe(true);
    yumi.handle('command:done', { command: 'start' });
    expect(yumi.getLines().map((l) => l.text)).toEqual(['Команда «Запустить» выполнена.']);
  });

  it('в консоли — последние 5 реплик, подписчики уведомляются', () => {
    const yumi = new Yumi(new MascotCore());
    const seen = vi.fn();
    yumi.subscribe(seen);
    for (let i = 0; i < 7; i++) yumi.handle('command:sent', { command: i % 2 ? 'stop' : 'start' });
    expect(yumi.getLines()).toHaveLength(5);
    expect(seen).toHaveBeenCalledTimes(7);
  });

  it('атаки схлопываются: не чаще раза в 1,5 с, последняя в окне доходит', () => {
    const yumi = new Yumi(new MascotCore());
    yumi.attack('attack:connect', { ip: '192.0.2.1', protocol: 'ssh' });
    yumi.attack('attack:connect', { ip: '192.0.2.2', protocol: 'ssh' });
    yumi.attack('attack:connect', { ip: '192.0.2.3', protocol: 'ssh' });
    expect(yumi.getLines().map((l) => l.text)).toEqual(['Новое подключение с 192.0.2.1 по ssh.']);
    vi.advanceTimersByTime(1500);
    expect(yumi.getLines().map((l) => l.text)).toEqual([
      'Новое подключение с 192.0.2.1 по ssh.',
      'Новое подключение с 192.0.2.3 по ssh.',
    ]);
  });

  it('команды не ждут окна атак — шторм не заглушает ответ оператору', () => {
    const yumi = new Yumi(new MascotCore());
    yumi.attack('attack:connect', { ip: '192.0.2.1', protocol: 'ssh' });
    yumi.attack('attack:connect', { ip: '192.0.2.2', protocol: 'ssh' });
    yumi.handle('command:done', { command: 'stop' });
    expect(yumi.getLines().at(-1)?.text).toBe('Команда «Остановить» выполнена.');
  });

  it('отмена удаляет схлопнутый хвост и позволяет принимать новые настоящие события', () => {
    const yumi = new Yumi(new MascotCore());
    yumi.attack('attack:connect', { ip: '192.0.2.1', protocol: 'ssh' });
    yumi.attack('attack:connect', { ip: '192.0.2.2', protocol: 'ssh' });
    yumi.cancelPendingAttack();
    vi.advanceTimersByTime(5000);
    expect(yumi.getLines()).toHaveLength(1);
    yumi.attack('attack:connect', { ip: '192.0.2.3', protocol: 'ssh' });
    expect(yumi.getLines().at(-1)?.text).toContain('192.0.2.3');
    yumi.dispose();
  });
});
