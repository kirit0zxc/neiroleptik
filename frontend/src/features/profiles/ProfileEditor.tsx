import { zodResolver } from '@hookform/resolvers/zod';
import { useEffect, useState } from 'react';
import { useFieldArray, useForm } from 'react-hook-form';
import { Link, useNavigate, useParams } from 'react-router';
import { ApiError } from '../../shared/api/client';
import { formatTime } from '../../shared/format/time';
import { ErrorState, Loading, Panel, ui } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { useProfile, useSaveProfile } from './api';
import { ALLOWED, emptyProfile, hasCredentials, LEVELS, ProfileSchema, servicesForLevel, type ProfileConfig, type ProfileInput } from './schema';
import s from './profiles.module.css';

const LEVEL_LABEL = { low: 'Low — порты и баннеры', medium: 'Medium — эмуляция SSH (Cowrie)', high: 'High — настоящая VM за SSH-шлюзом' } as const;

/** Создание и редактирование профиля (F8, F9). */
export function ProfileEditor() {
  const { id } = useParams();
  const existing = useProfile(id);
  if (id && existing.isPending) return <Loading what="профиль" />;
  if (id && existing.isError) return <ErrorState message={existing.error.message} onRetry={() => void existing.refetch()} />;
  // key — чтобы форма пересоздалась с данными нужного профиля.
  return <ProfileForm key={id ?? 'new'} id={id} initial={existing.data?.config ?? emptyProfile()} profile={existing.data} />;
}

function ProfileForm({ id, initial, profile }: {
  id?: string; initial: ProfileConfig; profile?: ReturnType<typeof useProfile>['data'];
}) {
  const me = useMe();
  const editable = canEdit(me?.role);
  const navigate = useNavigate();
  const save = useSaveProfile(id);
  const [saved, setSaved] = useState<number | null>(null);
  const form = useForm<ProfileInput, unknown, ProfileConfig>({ resolver: zodResolver(ProfileSchema), defaultValues: initial, mode: 'onTouched' });
  const services = useFieldArray({ control: form.control, name: 'services' });
  const creds = useFieldArray({ control: form.control, name: 'decoy_credentials' });
  const level = form.watch('level');
  const { errors } = form.formState;

  // Смена уровня: у Low нет входа — учётки убираем; сервисы, которые
  // уровню не подходят, заменяем набором по умолчанию.
  useEffect(() => {
    if (!hasCredentials(level) && creds.fields.length) creds.replace([]);
    const current = form.getValues('services');
    const next = servicesForLevel(level, current);
    if (next !== current) services.replace(next);
  }, [level, creds, services, form]);

  const onSubmit = form.handleSubmit((config) => save.mutate(config, {
    onSuccess: (p) => {
      setSaved(p.revision);
      if (!id) navigate(`/profiles/${encodeURIComponent(p.profile_id)}`, { replace: true });
    },
    // 422: ошибки сервера — к своим полям (путь после «config»).
    onError: (err) => {
      if (!(err instanceof ApiError) || err.status !== 422) return;
      for (const f of err.fields) {
        if (f.path[0] !== 'config' || f.path.length < 2) continue;
        form.setError(f.path.slice(1).join('.') as never, { type: 'server', message: f.message });
      }
    },
  }));
  const failure = save.error instanceof ApiError ? save.error.message : save.error ? 'Не удалось сохранить' : null;

  return (
    <>
      <p><Link to="/profiles">← Все профили</Link></p>
      <h1>{id ? `Профиль ${profile?.config.name ?? ''}` : 'Новый профиль'}</h1>
      <form onSubmit={onSubmit} noValidate aria-label="Профиль ловушки">
        <fieldset disabled={!editable} className={s.grid}>
          {!editable && <p className={s.note}>Роль «наблюдатель» — только просмотр.</p>}

          <Panel title="Основное">
            <Field label="Название" error={errors.name}>
              {(a) => <input {...a} {...form.register('name')} />}
            </Field>
            <Field label="Уровень взаимодействия" error={errors.level}>
              {(a) => (
                <select {...a} {...form.register('level')}>
                  {LEVELS.map((l) => <option key={l} value={l}>{LEVEL_LABEL[l]}</option>)}
                </select>
              )}
            </Field>
          </Panel>

          <Panel title="Сервисы">
            {errors.services?.root && <p className={s.error} role="alert">{errors.services.root.message}</p>}
            {errors.services?.message && <p className={s.error} role="alert">{errors.services.message}</p>}
            {services.fields.map((f, i) => (
              <div key={f.id} className={s.row} role="group" aria-label={`Сервис ${i + 1}`}>
                <Field label="Протокол" error={errors.services?.[i]?.protocol}>
                  {(a) => (
                    <select {...a} {...form.register(`services.${i}.protocol`)}>
                      {ALLOWED[level].map((p) => <option key={p} value={p}>{p.toUpperCase()}</option>)}
                    </select>
                  )}
                </Field>
                <Field label="Порт" error={errors.services?.[i]?.port}>
                  {(a) => <input {...a} inputMode="numeric" {...form.register(`services.${i}.port`)} />}
                </Field>
                <Field label="Баннер" error={errors.services?.[i]?.banner} wide>
                  {(a) => <input {...a} {...form.register(`services.${i}.banner`)} />}
                </Field>
                <button type="button" className={s.remove} onClick={() => services.remove(i)}
                  disabled={services.fields.length === 1} aria-label={`Удалить сервис ${i + 1}`}>×</button>
              </div>
            ))}
            {level !== 'high' && (
              <button type="button" className={ui.button}
                onClick={() => services.append({ protocol: ALLOWED[level][0]!, port: '' as unknown as number, banner: '' })}>
                + Сервис
              </button>
            )}
          </Panel>

          {hasCredentials(level) && (
            <Panel title="Учётные данные-приманки">
              <p className={s.note}>Ненастоящие логины и пароли, которые «подойдут» атакующему.</p>
              {creds.fields.map((f, i) => (
                <div key={f.id} className={s.row} role="group" aria-label={`Учётка ${i + 1}`}>
                  <Field label="Логин" error={errors.decoy_credentials?.[i]?.username}>
                    {(a) => <input {...a} autoComplete="off" {...form.register(`decoy_credentials.${i}.username`)} />}
                  </Field>
                  <Field label="Пароль" error={errors.decoy_credentials?.[i]?.password}>
                    {(a) => <input {...a} autoComplete="off" {...form.register(`decoy_credentials.${i}.password`)} />}
                  </Field>
                  <button type="button" className={s.remove} onClick={() => creds.remove(i)} aria-label={`Удалить учётку ${i + 1}`}>×</button>
                </div>
              ))}
              <button type="button" className={ui.button} onClick={() => creds.append({ username: '', password: '' })}>+ Учётка</button>
            </Panel>
          )}

          <Panel title="Легенда">
            <div className={s.row}>
              <Field label="Имя хоста" error={errors.legend?.hostname}>
                {(a) => <input {...a} {...form.register('legend.hostname')} />}
              </Field>
              <Field label="ОС" error={errors.legend?.os}>
                {(a) => <input {...a} {...form.register('legend.os')} />}
              </Field>
            </div>
          </Panel>

          <Panel title="Логирование и канал">
            <div className={s.row}>
              <Field label="Лимит payload, КиБ" error={errors.logging?.payload_limit_kb}>
                {(a) => <input {...a} inputMode="numeric" {...form.register('logging.payload_limit_kb')} />}
              </Field>
              <label className={s.check}>
                <input type="checkbox" {...form.register('logging.record_tty')} /> Записывать терминал
              </label>
              <Field label="Интервал beacon, с" error={errors.channel?.beacon_interval_s}>
                {(a) => <input {...a} inputMode="numeric" {...form.register('channel.beacon_interval_s')} />}
              </Field>
              <Field label="Джиттер, %" error={errors.channel?.jitter_pct}>
                {(a) => <input {...a} inputMode="numeric" {...form.register('channel.jitter_pct')} />}
              </Field>
            </div>
          </Panel>

          {editable && (
            <div className={s.actions}>
              <button type="submit" className={ui.button} disabled={save.isPending}>
                {save.isPending ? 'Сохраняем…' : id ? 'Сохранить новую ревизию' : 'Создать профиль'}
              </button>
              {saved !== null && <span role="status" className={s.ok}>Сохранено: ревизия r{saved}</span>}
              {failure && <span role="alert" className={s.error}>{failure}</span>}
            </div>
          )}
        </fieldset>
      </form>

      {profile && (
        <Panel title="Ревизии">
          <ol className={s.revisions}>
            {[...profile.revisions].reverse().map((r) => (
              <li key={r.revision}><b>r{r.revision}</b> · {formatTime(r.created_at)} · {r.created_by}</li>
            ))}
          </ol>
        </Panel>
      )}
    </>
  );
}

let fieldSeq = 0;

/** Поле с подписью и ошибкой вне label (ошибка не входит в название поля). */
function Field({ label, error, wide, children }: {
  label: string; error?: { message?: string }; wide?: boolean;
  children: (a: { id: string; 'aria-invalid': boolean; 'aria-describedby'?: string }) => React.ReactNode;
}) {
  const [id] = useState(() => `f${++fieldSeq}`);
  return (
    <div className={wide ? `${s.field} ${s.wide}` : s.field}>
      <label htmlFor={id}>{label}</label>
      {children({ id, 'aria-invalid': !!error, 'aria-describedby': error ? `${id}-err` : undefined })}
      {error?.message && <span id={`${id}-err`} className={s.error}>{error.message}</span>}
    </div>
  );
}
