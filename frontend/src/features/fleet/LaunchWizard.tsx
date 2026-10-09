import { useMutation, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import { api, ApiError } from '../../shared/api/client';
import type { Deployment, Host, Profile } from '../../shared/api/types';
import { formatTime } from '../../shared/format/time';
import { ErrorState, Loading, Panel, StatusBadge, ui } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { useProfiles, useRevisions } from '../profiles/api';
import { hostChoice, profileFitsHost } from './launch';
import { hostStatus } from './status';
import s from './wizard.module.css';

const STEPS = ['Хост', 'Профиль', 'Проверка', 'Готово'] as const;
const LEVEL = { low: 'Low', medium: 'Medium', high: 'High' } as const;

/** Мастер запуска ловушки (F7): хост → профиль и ревизия → проверка → план. */
export function LaunchWizard() {
  // Наблюдатель пришёл по прямой ссылке — сразу объясняем (запрет всё равно у API).
  if (!canEdit(useMe()?.user.role)) {
    return <><h1>Запуск ловушки</h1><ErrorState message="Запускать ловушки могут только оператор и администратор." /></>;
  }
  return <Wizard />;
}

function Wizard() {
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: () => api<Host[]>('/api/v1/hosts') });
  const profiles = useProfiles();
  const [step, setStep] = useState(0);
  const [hostId, setHostId] = useState<string | null>(null);
  const [profileId, setProfileId] = useState<string | null>(null);
  const [revision, setRevision] = useState<number | null>(null);

  const host = hosts.data?.find((h) => h.id === hostId) ?? null;
  const profile = profiles.data?.find((p) => p.id === profileId) ?? null;
  const revisions = useRevisions(profileId ?? undefined);
  const unfit = host && profile ? profileFitsHost(profile.config.interaction_level, host) : null;

  const launch = useMutation({
    mutationFn: () => api<Deployment>('/api/v1/deployments', {
      method: 'POST',
      body: JSON.stringify({ host_id: hostId, profile_id: profileId, revision }),
    }),
    onSuccess: () => setStep(3),
  });

  const canNext = useMemo(() => {
    if (step === 0) return !!host && hostChoice(host).selectable;
    if (step === 1) return !!profile && revision !== null && !unfit;
    return false;
  }, [step, host, profile, revision, unfit]);

  if (hosts.isPending || profiles.isPending) return <Loading what="хосты и профили" />;
  if (hosts.isError) return <ErrorState message={hosts.error.message} onRetry={() => void hosts.refetch()} />;
  if (profiles.isError) return <ErrorState message={profiles.error.message} onRetry={() => void profiles.refetch()} />;

  const pickProfile = (p: Profile) => {
    setProfileId(p.id);
    setRevision(p.revision);
  };

  return (
    <>
      <p><Link to="/fleet">← Парк</Link></p>
      <h1>Запуск ловушки</h1>
      <ol className={s.steps} aria-label="Шаги">
        {STEPS.map((t, i) => (
          <li key={t} aria-current={i === step ? 'step' : undefined} className={i < step ? s.done : i === step ? s.current : undefined}>
            {i + 1}. {t}
          </li>
        ))}
      </ol>

      {step === 0 && (
        <Panel title="Где запустить">
          <fieldset className={s.choices}>
            <legend className={s.legend}>Хост</legend>
            {hosts.data.map((h) => {
              const c = hostChoice(h);
              const st = hostStatus(h);
              return (
                <label key={h.id} className={c.selectable ? s.choice : `${s.choice} ${s.disabled}`}>
                  <input type="radio" name="host" value={h.id} disabled={!c.selectable}
                    checked={hostId === h.id} onChange={() => setHostId(h.id)} />
                  <span className="mono">{h.id}</span>
                  <span>{h.name}</span>
                  <StatusBadge tone={st.tone}>{st.label}</StatusBadge>
                  <span className={s.caps}>{h.capabilities.join(', ')}</span>
                  {c.note && <span className={s.note}>{c.note}</span>}
                </label>
              );
            })}
          </fieldset>
        </Panel>
      )}

      {step === 1 && (
        <Panel title="Что запустить">
          <fieldset className={s.choices}>
            <legend className={s.legend}>Профиль</legend>
            {profiles.data.map((p) => {
              const why = host ? profileFitsHost(p.config.interaction_level, host) : null;
              return (
                <label key={p.id} className={why ? `${s.choice} ${s.disabled}` : s.choice}>
                  <input type="radio" name="profile" value={p.id} disabled={!!why}
                    checked={profileId === p.id} onChange={() => pickProfile(p)} />
                  <span>{p.name}</span>
                  <span className={s.caps}>{LEVEL[p.config.interaction_level]} · r{p.revision}</span>
                  {why && <span className={s.note}>{why}</span>}
                </label>
              );
            })}
          </fieldset>
          {profile && (
            <div className={s.revision}>
              <label htmlFor="rev">Ревизия</label>
              <select id="rev" value={revision ?? ''} onChange={(e) => setRevision(Number(e.target.value))}>
                {(revisions.data ?? [{ revision: profile.revision, created_at: profile.created_at, config: profile.config }])
                  .slice().sort((a, b) => b.revision - a.revision).map((r) => (
                    <option key={r.revision} value={r.revision}>
                      r{r.revision}{r.revision === profile.revision ? ' (последняя)' : ''} · {formatTime(r.created_at)}
                    </option>
                  ))}
              </select>
            </div>
          )}
        </Panel>
      )}

      {step === 2 && host && profile && (
        <Panel title="Проверьте перед запуском">
          <dl className={s.summary}>
            <dt>Хост</dt><dd className="mono">{host.id} · {host.name}</dd>
            <dt>Профиль</dt><dd>{profile.name} · ревизия r{revision}</dd>
            <dt>Уровень</dt><dd>{LEVEL[profile.config.interaction_level]}</dd>
            <dt>Сервисы</dt><dd className="mono">{profile.config.services.map((x) => `${x.protocol}/${x.port}`).join(', ')}</dd>
            <dt>Легенда</dt><dd className="mono">{profile.config.hostname ?? '—'}</dd>
          </dl>
          {launch.error && (
            <p role="alert" className={s.err}>{launch.error instanceof ApiError ? launch.error.message : 'Запуск не отправлен'}</p>
          )}
        </Panel>
      )}

      {step === 3 && (
        <Panel title="Готово">
          {launch.data
            ? <p className={s.ok}>Ловушка <b className="mono">{launch.data.id}</b> запланирована (r{launch.data.revision}). <Link to="/fleet">Перейти в парк</Link></p>
            : <p role="status">Отправляем…</p>}
        </Panel>
      )}

      {step < 3 && (
        <div className={s.nav}>
          {step > 0 && <button className={ui.button} onClick={() => setStep(step - 1)}>Назад</button>}
          {step < 2 && <button className={ui.button} disabled={!canNext} onClick={() => setStep(step + 1)}>Далее</button>}
          {step === 2 && (
            <button className={ui.button} disabled={launch.isPending} onClick={() => launch.mutate()}>
              {launch.isPending ? 'Отправляем…' : 'Запланировать'}
            </button>
          )}
        </div>
      )}
    </>
  );
}
