import { useQuery } from '@tanstack/react-query';
import { useCallback, useState } from 'react';
import { Link } from 'react-router';
import { api } from '../../shared/api/client';
import type { HfEvent } from '../../shared/api/types';
import { formatTime } from '../../shared/format/time';
import { useStreamEvents } from '../../shared/stream/stream';
import { Empty, ErrorState, Loading, Panel, ui } from '../../shared/ui';
import { describeEvent, eventLabel } from '../events/describe';
import s from './overview.module.css';

/**
 * Ответ сводки (превью этапа 4: `/analytics/summary` в контракте стадии 1
 * отсутствует, обслуживается моками).
 */
export interface Summary {
  deployments: { total: number; running: number; failed: number };
  hosts: { total: number; online: number; offline: number };
  events_last_hour: number;
  alerts_open: number;
  top_sources: { ip: string; count: number }[];
}

const RECENT = 10;

/** Обзор: плитки состояния, последние события, активные источники. */
export function OverviewPage() {
  const summary = useQuery({
    queryKey: ['summary'],
    queryFn: () => api<Summary>('/api/v1/analytics/summary'),
    refetchInterval: 15_000,
  });

  return (
    <>
      <h1>Обзор</h1>
      {summary.isPending ? <Loading what="сводку" />
        : summary.isError ? <ErrorState message={summary.error.message} onRetry={() => void summary.refetch()} />
        : <Tiles data={summary.data} />}
      <div className={s.columns}>
        <RecentEvents />
        <Panel title="Активные источники за час">
          {summary.data && summary.data.top_sources.length ? (
            <ol className={s.sources}>
              {summary.data.top_sources.map((t) => (
                <li key={t.ip}>
                  <Link className="mono" to={`/events?source_ip=${encodeURIComponent(t.ip)}`}>{t.ip}</Link>
                  <span className={s.count}>{t.count}</span>
                </li>
              ))}
            </ol>
          ) : <Empty>За последний час атак не было.</Empty>}
        </Panel>
      </div>
    </>
  );
}

function Tiles({ data }: { data: Summary }) {
  const { deployments: d, hosts: h } = data;
  return (
    <div className={s.tiles}>
      <Tile to="/fleet" title="Ловушки" value={`${d.running} из ${d.total}`} caption="работают"
        alert={d.failed ? { tone: 'bad', text: `упали: ${d.failed}` } : null} />
      <Tile to="/fleet" title="Хосты" value={`${h.online} из ${h.total}`} caption="на связи"
        alert={h.offline ? { tone: 'warn', text: `offline: ${h.offline}` } : null} />
      <Tile to="/events" title="События за час" value={String(data.events_last_hour)} caption="от всех ловушек" alert={null} />
      <Tile to="/alerts" title="Открытые алерты" value={String(data.alerts_open)} caption="требуют внимания"
        alert={data.alerts_open ? { tone: 'bad', text: 'есть новые' } : null} />
    </div>
  );
}

function Tile({ to, title, value, caption, alert }: {
  to: string; title: string; value: string; caption: string;
  alert: { tone: 'warn' | 'bad'; text: string } | null;
}) {
  return (
    <Link to={to} className={`win ${s.tile}`} aria-label={`${title}: ${value} ${caption}${alert ? `, ${alert.text}` : ''}`}>
      <span className={`win-titlebar ${s.title}`}><span className="win-icon" aria-hidden="true" />{title}</span>
      <span className={s.tileBody}>
        <span className={s.value}>{value}</span>
        <span className={s.caption}>{caption}</span>
        {alert && <span className={alert.tone === 'bad' ? s.bad : s.warn}>{alert.text}</span>}
      </span>
    </Link>
  );
}

/** 10 последних событий: из REST, новые — из потока сверху. */
function RecentEvents() {
  const q = useQuery({
    queryKey: ['events-recent'],
    queryFn: () => api<{ items: HfEvent[] }>(`/api/v1/events?limit=${RECENT}`),
  });
  const [live, setLive] = useState<HfEvent[]>([]);
  const onLive = useCallback((events: HfEvent[]) => {
    setLive((prev) => [...[...events].reverse(), ...prev].slice(0, RECENT));
  }, []);
  useStreamEvents(onLive);

  const seen = new Set<string>();
  const items = [...live, ...(q.data?.items ?? [])].filter((e) => !seen.has(e.event_id) && !!seen.add(e.event_id)).slice(0, RECENT);

  return (
    <Panel title="Последние события">
      {q.isPending ? <Loading what="события" />
        : q.isError ? <ErrorState message={q.error.message} onRetry={() => void q.refetch()} />
        : !items.length ? <Empty>Событий пока нет.</Empty>
        : (
          <table className={ui.table}>
            <tbody>
              {items.map((e) => (
                <tr key={e.event_id} data-testid="recent-row">
                  <td>{formatTime(e.occurred_at)}</td>
                  <td>{eventLabel(e.type)}</td>
                  <td className="mono">{e.source.ip}</td>
                  <td className="mono">{describeEvent(e)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      <p className={s.more}><Link to="/events">Вся лента →</Link></p>
    </Panel>
  );
}
