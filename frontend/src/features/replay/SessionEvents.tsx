import { useEffect, useState } from 'react';
import { api } from '../../shared/api/client';
import type { HfEvent } from '../../shared/api/types';
import { eventKey } from '../../shared/api/types';
import { formatTime } from '../../shared/format/time';
import { Empty, ErrorState, Loading, Panel, ui } from '../../shared/ui';

interface Page { items: HfEvent[]; next_cursor: string | null }
const LABELS: Record<string, string> = {
  'connection.opened': 'Подключение', 'auth.attempt': 'Попытка входа',
  'session.command': 'Команда', 'session.output': 'Записанный обмен',
  'session.closed': 'Соединение закрыто', 'runtime.state': 'Событие ловушки',
};

/** История использует только публичную проекцию сервера; произвольный data не раскрывается. */
export function SessionEvents({ id }: { id: string }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const cursor = cursors.at(-1);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(false);
    const params = new URLSearchParams({ session_id: id, sort: 'occurred_at', order: 'asc', limit: '50' });
    if (cursor) params.set('cursor', cursor);
    void api<Page>(`/api/v1/events?${params}`, { signal: controller.signal }).then(
      (value) => { if (!controller.signal.aborted) setPage(value); },
      () => { if (!controller.signal.aborted) setError(true); },
    );
    return () => controller.abort();
  }, [id, cursor, retry]);

  return <Panel title="События сессии">
    <p>Опубликованные события; полнота захвата неизвестна. Запись терминала — ниже.</p>
    {error ? <ErrorState message="Не удалось загрузить события сессии." onRetry={() => setRetry((n) => n + 1)} />
      : !page ? <Loading what="события сессии" />
        : !page.items.length ? <Empty>События этой сессии пока не опубликованы.</Empty>
          : <div style={{ maxHeight: '22rem', overflow: 'auto' }}><table className={ui.table}>
            <thead><tr><th>Когда</th><th>Событие</th><th>Источник</th><th>Подробности</th></tr></thead>
            <tbody>{page.items.map((event) => <tr key={eventKey(event)}>
              <td>{formatTime(event.occurred_at)}</td>
              <td>{LABELS[event.type] ?? event.type}</td>
              <td>{event.source ? `${event.source.ip}:${event.source.port}` : 'неизвестен'}</td>
              <td style={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{details(event)}</td>
            </tr>)}</tbody>
          </table></div>}
    <div>
      <button className={ui.button} disabled={cursors.length === 1} onClick={() => setCursors((v) => v.slice(0, -1))}>Предыдущие события</button>{' '}
      <span>Страница {cursors.length}</span>{' '}
      <button className={ui.button} disabled={!page?.next_cursor} onClick={() => { if (page?.next_cursor) setCursors((v) => [...v, page.next_cursor]); }}>Следующие события</button>{' '}
      <button className={ui.button} onClick={() => { setCursors([null]); setRetry((n) => n + 1); }}>Обновить события</button>
    </div>
  </Panel>;
}

/** Даже случайно пришедший password не попадает в DOM; команды отображаются текстом. */
function details(event: HfEvent): string {
  if (event.type === 'session.command' && typeof event.data.input === 'string') {
    return event.data.input.replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '')
      + (event.data.truncated === true ? ' [обрезано]' : '');
  }
  if (event.type === 'auth.attempt') {
    return event.data.success === true ? 'Вход выполнен' : event.data.success === false ? 'Вход отклонён' : 'Результат неизвестен';
  }
  return '—';
}
