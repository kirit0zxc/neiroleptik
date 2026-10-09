/**
 * Безопасный текст из сессии атакующего (specs/frontend.md, S2).
 *
 * Вывод терминала может содержать escape-последовательности: смену
 * заголовка окна, гиперссылки OSC 8, перемещение курсора, запросы,
 * на которые эмулятор терминала «отвечает». Здесь мы ничего не
 * интерпретируем — удаляем всё управляющее, оставляем печатный текст,
 * перевод строки и табуляцию. Отображается результат только как текст.
 */

// Файл намеренно ищет управляющие символы — в этом его задача.
/* eslint-disable no-control-regex */

// OSC: ESC ] … (BEL | ESC \) — заголовки окна, ссылки.
const OSC = /\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)?/g;
// DCS / SOS / PM / APC: ESC P|X|^|_ … ESC \
const STRING_SEQ = /\u001b[PX^_][\s\S]*?(?:\u001b\\|$)/g;
// CSI: ESC [ параметры и финальный байт — цвет, курсор, очистка.
const CSI = /\u001b\[[0-?]*[ -/]*[@-~]/g;
// Восьмибитный CSI (0x9B) и прочие C1.
const C1_CSI = /\u009b[0-?]*[ -/]*[@-~]/g;
// Остальные двухбайтовые ESC-последовательности.
const ESC_OTHER = /\u001b[@-Z\\-_]?/g;
// Управляющие символы, кроме \t и \n; \r превращаем в перевод строки ниже.
const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g;

export function sanitizeTerminal(raw: string): string {
  return raw
    .replace(OSC, '')
    .replace(STRING_SEQ, '')
    .replace(CSI, '')
    .replace(C1_CSI, '')
    .replace(ESC_OTHER, '')
    .replace(/\r\n?/g, '\n')
    .replace(CONTROL, '');
}

/**
 * Маска учётных данных атакующего (S4): видна длина порядка и края,
 * но не значение. Пустое — явно «(пусто)», чтобы не путать с маской.
 */
export function maskSecret(value: string): string {
  if (!value) return '(пусто)';
  if (value.length <= 2) return '•'.repeat(value.length);
  return value[0] + '•'.repeat(Math.min(value.length - 2, 8)) + value.at(-1);
}
