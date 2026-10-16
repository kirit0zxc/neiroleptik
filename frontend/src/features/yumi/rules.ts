import type { BridgeRules, NormalizedPayload } from '../../mascot/bridge/types';
import { sanitizeTerminal } from '../../shared/security/terminal';
import { COMMAND_LABEL, type Command } from '../fleet/commands';
import { STORM_THRESHOLD } from './reactions';

/**
 * Реакции Юми на события HoneyForge (specs/frontend.md, раздел 9; таблица
 * продукта для specs/mascot.md, раздел 5.3). Экраны сообщают, что
 * произошло; что Юми при этом делает и говорит — решает только эта таблица.
 * Поля `repeat`/`variant`/`first`/`danger` проставляет `ReactionMemory`
 * (reactions.ts); без неё (прямой вызов, тесты) действуют значения по
 * умолчанию и реплики совпадают с исходными.
 */

/** Длина команды атакующего в реплике. */
const COMMAND_MAX = 80;

/** Вариант реплики по индексу: мусор и выход за край сводим к исходной (0). */
const pick = (variants: readonly string[], i: number) =>
  variants[((Math.trunc(i) % variants.length) + variants.length) % variants.length]!;

/** Склонение «попытка/попытки/попыток» по числу. */
const tries = (n: number) => {
  const d = n % 10;
  const dd = n % 100;
  if (d === 1 && dd !== 11) return 'попытка';
  if (d >= 2 && d <= 4 && (dd < 12 || dd > 14)) return 'попытки';
  return 'попыток';
};

const commandName = (p: NormalizedPayload) => {
  const c = p.str('command');
  return Object.hasOwn(COMMAND_LABEL, c) ? COMMAND_LABEL[c as Command] : c || 'команда';
};

/** Данные атакующего — только печатный текст без управляющих символов (S1, S2). */
const safe = (value: string, max = 64) => {
  const clean = sanitizeTerminal(value).replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
};
/** Причина без конечной точки — реплика ставит свою. */
const reasonOf = (p: NormalizedPayload) => safe(p.str('reason'), 120).replace(/[.!?…\s]+$/, '');
const ip = (p: NormalizedPayload) => safe(p.str('ip'), 45) || 'неизвестного адреса';

export const YUMI_RULES: BridgeRules = {
  'app:welcome': { state: 'welcome', say: 'Я на посту. Слежу за ловушками.' },

  'command:sent': (p) => ({ state: 'thinking', say: `Отправила команду «${commandName(p)}». Жду агента.` }),
  'command:done': (p) => ({ state: 'success', say: `Команда «${commandName(p)}» выполнена.` }),
  'command:failed': (p) => {
    const reason = reasonOf(p);
    return { state: 'error', say: `Команда «${commandName(p)}» не выполнена${reason ? `: ${reason}` : ''}.` };
  },
  'command:expired': (p) => ({ state: 'gentle', say: `Агент не выполнил «${commandName(p)}» вовремя — проверь хост.` }),
  'command:rejected': (p) => {
    const reason = reasonOf(p);
    return { state: 'gentle', say: `Центр не принял команду «${commandName(p)}»${reason ? `: ${reason}` : ''}.` };
  },
  'command:unknown': { state: 'curious', say: 'Не знаю, дошла ли команда. Повтор безопасен.' },

  'stream:lost': { state: 'error', say: 'Потеряла связь с потоком событий, переподключаюсь.' },
  'stream:restored': { state: 'welcome', say: 'Связь вернулась, пропущенное догрузила.' },

  'attack:connect': (p) => {
    const addr = ip(p);
    const proto = safe(p.str('protocol'), 16) || 'неизвестному протоколу';
    const repeat = p.num('repeat', 1);
    // Поток подключений с одного адреса — Юми осматривается (searching, не thinking).
    if (repeat >= STORM_THRESHOLD) return { state: 'searching', say: `Шквал подключений с ${addr} — уже ${repeat}.` };
    return {
      state: 'curious',
      say: pick([
        `Новое подключение с ${addr} по ${proto}.`,
        `Снова стучатся с ${addr} по ${proto}.`,
        `Ещё подключение с ${addr} (${proto}).`,
      ], p.num('variant', 0)),
    };
  },
  'attack:auth': (p) => {
    const addr = ip(p);
    const user = safe(p.str('username'), 32) || 'без логина';
    if (p.bool('success')) {
      // Первый прорыв в ловушку за сессию — кульминация: Юми радуется (танец `67-2`).
      return p.bool('first')
        ? { state: '67-2', say: `Прорыв! ${addr} вошёл как ${user} — ловушка сработала, пошла живая сессия!` }
        : { state: 'excited', say: `${addr} вошёл как ${user}!` };
    }
    const repeat = p.num('repeat', 1);
    if (repeat >= STORM_THRESHOLD) return { state: 'error', say: `Перебор паролей с ${addr} — уже ${repeat} ${tries(repeat)}.` };
    // Подбор паролей — Юми наблюдает, осматриваясь (searching), а не «думает».
    return {
      state: 'searching',
      say: pick([
        `${addr} пробует войти как ${user}.`,
        `${addr} подбирает доступ, логин ${user}.`,
        `Ещё попытка входа с ${addr} (${user}).`,
      ], p.num('variant', 0)),
    };
  },
  'attack:command': (p) => {
    const addr = ip(p);
    const cmd = safe(p.str('command'), COMMAND_MAX) || '—';
    // Усечение на сервере (data.truncated) — тоже «…», если safe() его ещё не поставил.
    const cut = p.bool('truncated') && cmd !== '—' && !cmd.endsWith('…') ? '…' : '';
    if (p.bool('danger')) return { state: 'error', say: `Опасная команда с ${addr}: ${cmd}${cut}` };
    // Разбор обычной команды: состояние и реплика чередуются, чтобы не залипать на «думает».
    const i = p.num('variant', 0);
    return {
      state: pick(['analyzing', 'comparing', 'curious'], i),
      say: pick([
        `${addr} выполняет: ${cmd}${cut}`,
        `${addr} запускает: ${cmd}${cut}`,
        `В сессии с ${addr}: ${cmd}${cut}`,
      ], i),
    };
  },
  'attack:http': (p) => {
    const method = safe(p.str('method'), 8);
    return { state: 'searching', say: `${ip(p)} шлёт ${method ? `HTTP-запрос ${method}` : 'HTTP-запрос'}.` };
  },
  'attack:tcp': (p) => {
    const n = p.num('bytes', -1);
    return { state: 'comparing', say: n > 0 ? `${ip(p)} шлёт ${n} байт по TCP.` : `${ip(p)} шлёт данные по TCP.` };
  },
  // Медовый токен — пойман с поличным: Юми торжествует (пресет победы), реплика тревожная.
  'token:triggered': (p) => ({ state: 'victory_demo', say: `Сработал медовый токен! Источник ${ip(p)}.` }),
  'trap:offline': { state: 'gentle', say: 'Одна из ловушек остановилась.' },
};
