import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';
import { api, ApiError } from '../../shared/api/client';
import type { Deployment, Host, Profile } from '../../shared/api/types';
import { formatTime } from '../../shared/format/time';
import { Empty, ErrorState, Loading, Panel, StatusBadge, ui } from '../../shared/ui';
import { useMe } from '../auth/auth';
import { actionInfo, describeDetails, shortId, whoLabel, type AuditEntry } from './describe';
import s from './audit.module.css';

const LIMIT = 50;
const DENIED = 'Журнал аудита доступен только администратору.';

/** Журнал аудита (F19): кто, что, когда, результат. Только admin. */
export function AuditPage() {
  const me = useMe();
  const isAdmin = me?.user.role === 'admin';
  return (
    <>
      <h1>Журнал аудита</h1>
      {isAdmin ? <AuditLog me={me.user} /> : <Empty>{DENIED}</Empty>}
    </>
  );
}

function AuditLog({ me }: { me: { id: string; username: string } }) {
  const [search, setSearch] = useSearchParams();
  const page = Math.max(0, Number.parseInt(search.get('page') ?? '0', 10) || 0);
  const audit = useQuery({
    queryKey: ['audit', page],
    queryFn: () => api<AuditEntry[]>(`/api/v1/audit?limit=${LIMIT}&offset=${page * LIMIT}`),
  });
  // Справочники для имён: те же списки, что у парка и профилей. Не загрузились —
  // журнал показывает id и не ломается.
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: () => api<Host[]>('/api/v1/hosts') });
  const profiles = useQuery({ queryKey: ['profiles'], queryFn: () => api<Profile[]>('/api/v1/profiles') });
  const deps = useQuery({ queryKey: ['deployments'], queryFn: () => api<Deployment[]>('/api/v1/deployments') });
  const hostNames = new Map((hosts.data ?? []).map((h) => [h.id, h.name]));
  const profileNames = new Map((profiles.data ?? []).map((p) => [p.id, p.name]));
  const deploymentIds = new Set((deps.data ?? []).map((d) => d.id));

  const go = (next: number) => setSearch((p) => {
    const q = new URLSearchParams(p);
    if (next > 0) q.set('page', String(next)); else q.delete('page');
    return q;
  });

  const target = (id: string | null) => {
    if (id === null) return '—';
    const profile = profileNames.get(id);
    if (profile) return <Link to={`/profiles/${encodeURIComponent(id)}`}>{profile}</Link>;
    if (deploymentIds.has(id)) return <Link to="/fleet" className="mono">ловушка {shortId(id)}</Link>;
    const host = hostNames.get(id);
    if (host) return `хост ${host}`;
    return <span className="mono" title={id}>{shortId(id)}</span>;
  };

  if (audit.isPending) return <Loading what="журнал" />;
  if (audit.isError) {
    const denied = audit.error instanceof ApiError && audit.error.status === 403;
    return denied ? <Empty>{DENIED}</Empty> : <ErrorState message={audit.error.message} onRetry={() => void audit.refetch()} />;
  }
  const rows = audit.data;
  return (
    <Panel title="Записи">
      {!rows.length ? <Empty>{page ? 'На этой странице записей нет.' : 'Записей пока нет.'}</Empty> : (
        <div className={s.scroll}>
          <table className={ui.table}>
            <thead><tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Объект</th><th>Подробности</th><th>Результат</th></tr></thead>
            <tbody>
              {rows.map((e) => {
                const info = actionInfo(e);
                const who = whoLabel(e.actor_id, me, hostNames);
                return (
                  <tr key={e.id}>
                    <td className="mono">{formatTime(e.created_at)}</td>
                    <td title={e.actor_id ?? undefined}>{who}</td>
                    <td>{info.label}</td>
                    <td>{target(e.target_id)}</td>
                    {/* Подробности — только текстом (S1). */}
                    <td className={s.details}>{describeDetails(e.details).join(' · ') || '—'}</td>
                    <td>
                      {info.ok === null ? <StatusBadge tone="idle">неизвестно</StatusBadge>
                        : info.ok ? <StatusBadge tone="ok">успех</StatusBadge>
                        : <StatusBadge tone="bad">ошибка</StatusBadge>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <nav className={s.pager} aria-label="Страницы журнала">
        <button className={ui.button} disabled={page === 0} onClick={() => go(page - 1)}>← Новее</button>
        <span>Страница {page + 1}</span>
        {/* Общего числа записей API не отдаёт: «Старее» — если пришла полная страница. */}
        <button className={ui.button} disabled={rows.length < LIMIT} onClick={() => go(page + 1)}>Старее →</button>
      </nav>
    </Panel>
  );
}
