import type { BridgeRules, NormalizedPayload } from '../../mascot/bridge/types';
import { sanitizeTerminal } from '../../shared/security/terminal';
import { COMMAND_LABEL, type Command } from '../fleet/commands';

/**
 * Реакции Юми на события HoneyForge (specs/frontend.md, раздел 9; таблица
 * продукта для specs/mascot.md, раздел 5.3). Экраны сообщают, что
 * произошло; что Юми при этом делает и говорит — решает только эта таблица.
 */

/** Длина команды атакующего в реплике. */
const COMMAND_MAX = 80;

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

  'attack:connect': (p) => ({ state: 'curious', say: `Новое подключение с ${ip(p)} по ${safe(p.str('protocol'), 16) || 'неизвестному протоколу'}.` }),
  'attack:auth': (p) => {
    const user = safe(p.str('username'), 32) || 'без логина';
    return p.bool('success')
      ? { state: 'excited', say: `${ip(p)} вошёл как ${user}!` }
      : { state: 'analyzing', say: `${ip(p)} пробует войти как ${user}.` };
  },
  'attack:command': (p) => {
    const cmd = safe(p.str('command'), COMMAND_MAX) || '—';
    // Усечение на сервере (data.truncated) — тоже «…», если safe() его ещё не поставил.
    const cut = p.bool('truncated') && cmd !== '—' && !cmd.endsWith('…') ? '…' : '';
    return { state: 'analyzing', say: `${ip(p)} выполняет: ${cmd}${cut}` };
  },
  'attack:http': (p) => {
    const method = safe(p.str('method'), 8);
    return { state: 'analyzing', say: `${ip(p)} отправил ${method ? `HTTP-запрос ${method}` : 'HTTP-запрос'}.` };
  },
  'attack:tcp': (p) => {
    const n = p.num('bytes', -1);
    return { state: 'analyzing', say: n >= 0 ? `${ip(p)} прислал ${n} байт по TCP.` : `${ip(p)} прислал данные по TCP.` };
  },
  'token:triggered': (p) => ({ state: 'error', say: `Сработал медовый токен! Источник ${ip(p)}.` }),
  'trap:offline': { state: 'gentle', say: 'Одна из ловушек остановилась.' },
};
