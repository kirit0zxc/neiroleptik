import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { api } from '../../shared/api/client';
import type { Deployment, Host, PreviewDeployment } from '../../shared/api/types';
import { ago } from '../../shared/format/time';
import { Empty, ErrorState, Loading, Panel, StatusBadge, ui } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { DeploymentActions } from './DeploymentActions';
import { deploymentStatus, hostStatus } from './status';

const LEVEL = { low: 'Low', medium: 'Medium', high: 'High' } as const;

/** Превью распознаём только в явно включённом демо; реальный API — DeploymentRead. */
function previewOf(deployment: Deployment | PreviewDeployment): PreviewDeployment | null {
  return import.meta.env.VITE_API_MOCK === '1' && 'desired_revision' in deployment
    ? deployment : null;
}

/** Парк: хосты и ловушки (F4, F5), команды ловушек (F6). Мастер запуска — D3. */
export function FleetPage() {
  const editable = canEdit(useMe()?.user.role);
  const commandsVisible = editable && import.meta.env.VITE_API_MOCK === '1';
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: () => api<Host[]>('/api/v1/hosts'), refetchInterval: 10_000 });
  const deps = useQuery({ queryKey: ['deployments'], queryFn: () => api<Array<Deployment | PreviewDeployment>>('/api/v1/deployments'), refetchInterval: 10_000 });

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
              <thead><tr><th>Хост</th><th>Имя</th><th>Связь</th><th>Последний контакт</th><th>Возможности</th></tr></thead>
              <tbody>
                {hosts.data.map((h) => {
                  const st = hostStatus(h);
                  return (
                    <tr key={h.id}>
                      <td className="mono">{h.id}</td>
                      <td>{h.name}</td>
                      <td><StatusBadge tone={st.tone}>{st.label}</StatusBadge></td>
                      <td>{h.last_seen ? ago(h.last_seen) : 'ещё не выходил'}</td>
                      <td className="mono">{h.capabilities.join(', ') || '—'}</td>
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
              <thead><tr><th>Ловушка</th><th>Уровень</th><th>Хост</th><th>Состояние</th><th>Профиль</th>{commandsVisible && <th>Действия</th>}</tr></thead>
              <tbody>
                {deps.data.map((d) => {
                  const preview = previewOf(d);
                  const st = preview ? deploymentStatus(preview) : { tone: 'idle', label: 'запланирована' } as const;
                  // DeploymentRead не содержит уровень: не подменяем его текущим профилем.
                  const revision = preview?.desired_revision ?? ('revision' in d ? d.revision : '—');
                  return (
                    <tr key={d.id}>
                      <td className="mono">{d.id}</td>
                      <td>{preview ? LEVEL[preview.interaction_level] : '—'}</td>
                      <td className="mono">{d.host_id}</td>
                      <td><StatusBadge tone={st.tone}>{st.label}</StatusBadge></td>
                      <td className="mono">{d.profile_id} · r{revision}</td>
                      {commandsVisible && <td>{preview && <DeploymentActions deployment={preview} />}</td>}
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
