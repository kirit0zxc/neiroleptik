import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { api } from '../../shared/api/client';
import type { Deployment, Host } from '../../shared/api/types';
import { ago } from '../../shared/format/time';
import { Empty, ErrorState, Loading, Panel, StatusBadge, ui } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { DeploymentActions } from './DeploymentActions';
import { deploymentStatus, hostStatus } from './status';

const LEVEL = { low: 'Low', medium: 'Medium', high: 'High' } as const;

/** Парк: хосты и ловушки (F4, F5), команды ловушек (F6). Мастер запуска — D3. */
export function FleetPage() {
  const editable = canEdit(useMe()?.role);
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: () => api<Host[]>('/api/v1/hosts'), refetchInterval: 10_000 });
  const deps = useQuery({ queryKey: ['deployments'], queryFn: () => api<Deployment[]>('/api/v1/deployments'), refetchInterval: 10_000 });

  return (
    <>
      <h1>Парк ловушек</h1>
      {editable && <p><Link className={ui.button} to="/fleet/new">+ Запустить ловушку</Link></p>}
      <Panel title="Хосты">
        {hosts.isPending ? <Loading what="хосты" />
          : hosts.isError ? <ErrorState message={hosts.error.message} onRetry={() => void hosts.refetch()} />
          : !hosts.data.length ? <Empty>Хостов пока нет — зарегистрируйте агента.</Empty>
          : (
            <table className={ui.table}>
              <thead><tr><th>Хост</th><th>Связь</th><th>Последний контакт</th><th>Очередь</th><th>Свободно</th></tr></thead>
              <tbody>
                {hosts.data.map((h) => {
                  const st = hostStatus(h);
                  return (
                    <tr key={h.host_id}>
                      <td className="mono">{h.host_id}</td>
                      <td><StatusBadge tone={st.tone}>{st.label}</StatusBadge></td>
                      <td>{ago(h.last_seen)}</td>
                      <td className="mono">{h.backlog}</td>
                      <td className="mono">{(h.disk_free_mb / 1024).toFixed(1)} ГБ</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
      </Panel>
      <Panel title="Ловушки">
        {deps.isPending ? <Loading what="ловушки" />
          : deps.isError ? <ErrorState message={deps.error.message} onRetry={() => void deps.refetch()} />
          : !deps.data.length ? <Empty>Ловушек пока нет.</Empty>
          : (
            <table className={ui.table}>
              <thead><tr><th>Ловушка</th><th>Уровень</th><th>Хост</th><th>Состояние</th><th>Профиль</th>{editable && <th>Действия</th>}</tr></thead>
              <tbody>
                {deps.data.map((d) => {
                  const st = deploymentStatus(d);
                  return (
                    <tr key={d.deployment_id}>
                      <td className="mono">{d.deployment_id}</td>
                      <td>{LEVEL[d.interaction_level] ?? d.interaction_level}</td>
                      <td className="mono">{d.host_id}</td>
                      <td><StatusBadge tone={st.tone}>{st.label}</StatusBadge></td>
                      <td className="mono">{d.profile_id} · r{d.desired_revision}</td>
                      {editable && <td><DeploymentActions deployment={d} /></td>}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
      </Panel>
    </>
  );
}
