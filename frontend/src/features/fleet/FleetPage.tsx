import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { api } from '../../shared/api/client';
import type { Deployment, Host } from '../../shared/api/types';
import { ago } from '../../shared/format/time';
import { Empty, ErrorState, Loading, Panel, StatusBadge, ui } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { DeploymentActions } from './DeploymentActions';
import { AddHostButton, RevokeHostButton } from './HostAdmin';
import { deploymentStatus, hostStatus, hostUnreachable } from './status';
import s from './fleet.module.css';

const LEVEL = { low: 'Low', medium: 'Medium', high: 'High' } as const;

/** Парк: хосты и ловушки (F4, F5), команды ловушек (F6) — только operator/admin. */
export function FleetPage() {
  const role = useMe()?.user.role;
  const editable = canEdit(role);
  // Регистрация агентов и отзыв хостов — только admin (specs, «Регистрация агента»).
  const admin = role === 'admin';
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: () => api<Host[]>('/api/v1/hosts'), refetchInterval: 10_000 });
  const deps = useQuery({ queryKey: ['deployments'], queryFn: () => api<Deployment[]>('/api/v1/deployments'), refetchInterval: 10_000 });

  return (
    <>
      <h1>Парк ловушек</h1>
      {editable && <p><Link className={ui.button} to="/fleet/new">+ Запустить ловушку</Link></p>}
      <Panel title="Хосты">
        {/* div, а не p: рядом с кнопкой рендерится окно с <pre> — внутри <p> это невалидно. */}
        {admin && <div className={s.toolbar}><AddHostButton /></div>}
        {hosts.isError && hosts.data && <Stale what="хостов" message={hosts.error.message} onRetry={() => void hosts.refetch()} />}
        {hosts.isPending ? <Loading what="хосты" />
          : !hosts.data ? <ErrorState message={hosts.error?.message ?? 'Не удалось загрузить'} onRetry={() => void hosts.refetch()} />
          : !hosts.data.length ? <Empty>{admin ? 'Хостов пока нет — добавьте хост и зарегистрируйте агента.' : 'Хостов пока нет — администратор регистрирует агентов.'}</Empty>
          : (
            <table className={ui.table}>
              <thead><tr><th>Хост</th><th>Имя</th><th>Связь</th><th>Последний контакт</th><th>Возможности</th>{admin && <th>Действия</th>}</tr></thead>
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
                      {admin && <td><RevokeHostButton host={h} /></td>}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
      </Panel>
      <Panel title="Ловушки">
        {/* Сбой обновления не убирает таблицу: в её строках идут задания, их опрос и ключи повтора. */}
        {deps.isError && deps.data && <Stale what="ловушек" message={deps.error.message} onRetry={() => void deps.refetch()} />}
        {deps.isPending ? <Loading what="ловушки" />
          : !deps.data ? <ErrorState message={deps.error?.message ?? 'Не удалось загрузить'} onRetry={() => void deps.refetch()} />
          : !deps.data.length ? <Empty>Ловушек пока нет.</Empty>
          : (
            <table className={ui.table}>
              <thead><tr><th>Ловушка</th><th>Уровень</th><th>Хост</th><th>Состояние</th><th>Профиль</th>{editable && <th>Действия</th>}</tr></thead>
              <tbody>
                {deps.data.map((d) => {
                  const st = deploymentStatus(d);
                  const unreachable = hostUnreachable(hosts.data?.find((h) => h.id === d.host_id)?.status);
                  return (
                    <tr key={d.id}>
                      <td className="mono">{d.id}</td>
                      {/* Уровень назначенной ревизии — из ответа, не из текущей версии профиля. */}
                      <td>{LEVEL[d.interaction_level] ?? '—'}</td>
                      <td className="mono">{d.host_id}</td>
                      <td>
                        <StatusBadge tone={st.tone}>{st.label}</StatusBadge>
                        {unreachable && <div className={s.unconfirmed}>хост не на связи — состояние не подтверждено</div>}
                      </td>
                      <td className="mono">{d.profile_id} · r{d.revision}</td>
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

/** Предупреждение поверх последних загруженных данных: обновить не удалось. */
function Stale({ what, message, onRetry }: { what: string; message: string; onRetry: () => void }) {
  return (
    // status, а не alert: при «мигающей» связи читалка не будет перебивать работу.
    <p role="status" className={s.stale}>
      Не удалось обновить список {what}: {message.replace(/[.s]+$/, '')}. Показаны последние данные.{' '}
      <button className={`${ui.button} ${s.small}`} onClick={onRetry}>Повторить</button>
    </p>
  );
}
