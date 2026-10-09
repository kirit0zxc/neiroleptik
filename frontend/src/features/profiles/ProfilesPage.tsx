import { Link } from 'react-router';
import { ago } from '../../shared/format/time';
import { Empty, ErrorState, Loading, Panel, ui } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { useProfiles } from './api';

const LEVEL = { low: 'Low', medium: 'Medium', high: 'High' } as const;

/** Список профилей (F8). */
export function ProfilesPage() {
  const me = useMe();
  const q = useProfiles();
  return (
    <>
      <h1>Профили ловушек</h1>
      {canEdit(me?.role) && <p><Link className={ui.button} to="/profiles/new">+ Новый профиль</Link></p>}
      <Panel title="Профили">
        {q.isPending ? <Loading what="профили" />
          : q.isError ? <ErrorState message={q.error.message} onRetry={() => void q.refetch()} />
          : !q.data.length ? <Empty>Профилей пока нет — создайте первый.</Empty>
          : (
            <table className={ui.table}>
              <thead><tr><th>Название</th><th>Уровень</th><th>Сервисы</th><th>Ревизия</th><th>Изменён</th></tr></thead>
              <tbody>
                {q.data.map((p) => (
                  <tr key={p.profile_id}>
                    <td><Link to={`/profiles/${encodeURIComponent(p.profile_id)}`}>{p.config.name}</Link></td>
                    <td>{LEVEL[p.config.level]}</td>
                    <td className="mono">{p.config.services.map((s) => `${s.protocol}/${s.port}`).join(', ')}</td>
                    <td className="mono">r{p.revision}</td>
                    <td>{ago(p.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </Panel>
    </>
  );
}
