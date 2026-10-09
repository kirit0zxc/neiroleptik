// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { maskSecret, sanitizeTerminal } from './terminal';

const ESC = '\u001b';

describe('sanitizeTerminal (S2)', () => {
  it('убирает цвет и перемещение курсора, оставляет текст', () => {
    expect(sanitizeTerminal(`${ESC}[1;31mroot${ESC}[0m@host:~$ ${ESC}[2J${ESC}[H`)).toBe('root@host:~$ ');
  });

  it('убирает смену заголовка окна и гиперссылки OSC 8', () => {
    expect(sanitizeTerminal(`${ESC}]0;pwned\u0007ok`)).toBe('ok');
    expect(sanitizeTerminal(`${ESC}]8;;https://evil.test${ESC}\\click${ESC}]8;;${ESC}\\`)).toBe('click');
  });

  it('убирает DCS и восьмибитный CSI', () => {
    expect(sanitizeTerminal(`a${ESC}Pq#0;2;0;0;0${ESC}\\b`)).toBe('ab');
    expect(sanitizeTerminal('a\u009b31mb')).toBe('ab');
  });

  it('сохраняет перевод строки, табуляцию и юникод; \\r — в перевод строки', () => {
    expect(sanitizeTerminal('ls\r\nфайл\tтест\rok')).toBe('ls\nфайл\tтест\nok');
  });

  it('удаляет NUL, BEL, backspace и DEL', () => {
    expect(sanitizeTerminal('a\u0000b\u0007c\bd\u007fe')).toBe('abcde');
  });

  it('незавершённая последовательность: убирается только ESC, текст после неё остаётся', () => {
    expect(sanitizeTerminal(`${ESC}[31\nok`)).toBe('[31\nok');
  });

  it('HTML не трогает — экранирует его React при выводе как текста', () => {
    expect(sanitizeTerminal('<img src=x onerror=alert(1)>')).toBe('<img src=x onerror=alert(1)>');
  });
});

describe('maskSecret (S4)', () => {
  it('скрывает значение, оставляя края', () => {
    expect(maskSecret('hunter2')).toBe('h•••••2');
    expect(maskSecret('ab')).toBe('••');
    expect(maskSecret('')).toBe('(пусто)');
  });

  it('длинный пароль не раскрывает длину точно', () => {
    expect(maskSecret('a'.repeat(40))).toBe('a' + '•'.repeat(8) + 'a');
  });
});
