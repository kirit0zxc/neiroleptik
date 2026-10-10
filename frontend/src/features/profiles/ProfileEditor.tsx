import { zodResolver } from '@hookform/resolvers/zod';
import { useEffect, useState } from 'react';
import { useFieldArray, useForm } from 'react-hook-form';
import { Link, useLocation, useNavigate, useParams } from 'react-router';
import type { Profile } from '../../shared/api/types';
import { ApiError } from '../../shared/api/client';
import { formatTime } from '../../shared/format/time';
import { ErrorState, Loading, Panel, ui } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { useProfile, useRevisions, useSaveProfile } from './api';
import {
  ALLOWED, bannerLabel, emptyProfile, formFromConfig, formToCreate, hasCredentials, hasHigh, LEVELS,
  type Level, nextServiceProtocol, ProfileSchema, servicesForLevel, type ProfileForm, type ProfileInput,
} from './schema';
import s from './profiles.module.css';

const LEVEL_LABEL = { low: 'Low — порты и баннеры', medium: 'Medium — эмуляция SSH (Cowrie)', high: 'High — настоящая VM за SSH-шлюзом' } as const;

/** Создание и редактирование профиля (F8, F9). */
export function ProfileEditor() {
  const { id } = useParams();
  const existing = useProfile(id);
  if (id && existing.isPending) return <Loading what="профиль" />;
  if (id && existing.isError && !existing.data) return <ErrorState message={existing.error.message} onRetry={() => void existing.refetch()} />;
  const initial = existing.data
    ? formFromConfig(existing.data.name, existing.data.description, existing.data.config)
    : emptyProfile();
  // key — чтобы форма пересоздалась с данными нужного профиля.
  return <ProfileFormView key={id ?? 'new'} id={id} initial={initial} name={existing.data?.name} revision={existing.data?.revision} onReload={async () => {
    const result = await existing.refetch();
    if (result.error) throw result.error;
    return result.data;
  }} />;
}

function ProfileFormView({ id, initial, name, revision, onReload }: {
  id?: string; initial: ProfileForm; name?: string; revision?: number; onReload: () => Promise<Profile | undefined>;
}) {
  const me = useMe();
  const editable = canEdit(me?.user.role);
  const navigate = useNavigate();
  // Версия принадлежит снимку формы, а не последнему фоновому ответу запроса.
  const [snapshot, setSnapshot] = useState({ name, revision });
  const save = useSaveProfile(id, snapshot.revision);
  const [reloading, setReloading] = useState(false);
  const [reloadError, setReloadError] = useState<string | null>(null);
  const revisions = useRevisions(id);
  // После создания форма пересоздаётся на адресе нового профиля: номер
  // сохранённой ревизии приходит в состоянии перехода, чтобы сообщение не пропало.
  const location = useLocation();
  const [saved, setSaved] = useState<number | null>(() => (location.state as { saved?: number } | null)?.saved ?? null);
  const form = useForm<ProfileInput, unknown, ProfileForm>({ resolver: zodResolver(ProfileSchema), defaultValues: initial, mode: 'onTouched' });
  const services = useFieldArray({ control: form.control, name: 'services' });
  const creds = useFieldArray({ control: form.control, name: 'credentials' });
  const level = form.watch('level');
  const credentialsMode = form.watch('credentials_mode');
  const preservedUsernames = form.watch('preserved_usernames');
  const serviceValues = form.watch('services');
  const addProtocol = nextServiceProtocol(level, serviceValues);
  const { errors } = form.formState;

  // Открытый профиль мог быть создан в обход формы (например, два HTTP на Low):
  // показываем нарушения сразу, ничего не меняя в значениях.
  useEffect(() => {
    if (id) void form.trigger('services');
  }, [id, snapshot.revision, form]);

  // Явная смена уровня пользователем: у Low нет входа — учётки убираем;
  // сервисы, которые новому уровню не подходят, заменяем набором по умолчанию.
  // Только здесь, а не при открытии или правке: иначе форма молча подменяла
  // бы сохранённую конфигурацию (замечание к PR #9), а не показывала ошибку.
  const applyLevel = (next: Level) => {
    if (!hasCredentials(next)) {
      if (creds.fields.length) creds.replace([]);
      if (form.getValues('credentials_mode') !== 'replace') form.setValue('credentials_mode', 'replace');
      if (form.getValues('preserved_usernames').length) form.setValue('preserved_usernames', []);
    }
    // Скрытый блок VM не должен блокировать Low/Medium невидимой ошибкой.
    if (!hasHigh(next)) {
      form.setValue('high', emptyProfile(next).high);
      form.clearErrors('high');
    }
    const current = form.getValues('services');
    const replaced = servicesForLevel(next, current);
    if (replaced !== current) services.replace(replaced);
  };

  const onSubmit = form.handleSubmit((values) => save.mutate(formToCreate(values), {
    onSuccess: (p) => {
      setSaved(p.revision);
      setSnapshot({ name: p.name, revision: p.revision });
      form.reset(formFromConfig(p.name, p.description, p.config));
      if (!id) navigate(`/profiles/${encodeURIComponent(p.id)}`, { replace: true, state: { saved: p.revision } });
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
  // Перечитывание намеренно явное: конфликт не стирает несохранённые изменения.
  const reload = async () => {
    setReloading(true);
    setReloadError(null);
    try {
      const p = await onReload();
      if (!p) throw new Error('Не удалось перечитать профиль');
      setSnapshot({ name: p.name, revision: p.revision });
      form.reset(formFromConfig(p.name, p.description, p.config));
      save.reset();
      // Перечитанная ревизия может совпасть с прежней: эффект не сработает,
      // а reset снял ошибки — проверяем сервисы явно.
      void form.trigger('services');
      setSaved(null);
    } catch (err) {
      setReloadError(err instanceof Error ? err.message : 'Не удалось перечитать профиль');
    } finally {
      setReloading(false);
    }
  };
  const conflict = save.error instanceof ApiError && save.error.status === 409;
  const failure = save.error instanceof ApiError ? save.error.message : save.error ? 'Не удалось сохранить' : null;

  return (
    <>
      <p><Link to="/profiles">← Все профили</Link></p>
      <h1>{id ? `Профиль ${snapshot.name ?? ''}` : 'Новый профиль'}</h1>
      <form onSubmit={onSubmit} noValidate aria-label="Профиль ловушки">
        <fieldset disabled={!editable || save.isPending || reloading} className={s.grid}>
          {!editable && <p className={s.note}>Роль «наблюдатель» — только просмотр.</p>}

          <Panel title="Основное">
            <Field label="Название" error={errors.name}>
              {(a) => <input {...a} {...form.register('name')} />}
            </Field>
            <Field label="Описание" error={errors.description} wide>
              {(a) => <input {...a} {...form.register('description')} />}
            </Field>
            <Field label="Уровень взаимодействия" error={errors.level}>
              {(a) => (
                <select {...a} {...form.register('level', { onChange: (e: { target: { value: Level } }) => applyLevel(e.target.value) })}>
                  {LEVELS.map((l) => <option key={l} value={l}>{LEVEL_LABEL[l]}</option>)}
                </select>
              )}
            </Field>
            <Field label="Имя хоста (легенда)" error={errors.hostname}>
              {(a) => <input {...a} {...form.register('hostname')} />}
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
                <Field label={bannerLabel(serviceValues[i]?.protocol ?? '')} error={errors.services?.[i]?.banner} wide>
                  {(a) => <input {...a} {...form.register(`services.${i}.banner`)} />}
                </Field>
                <button type="button" className={s.remove} onClick={() => services.remove(i)}
                  disabled={services.fields.length === 1} aria-label={`Удалить сервис ${i + 1}`}>×</button>
              </div>
            ))}
            {/* Живой регион есть всегда: читалка объявит подсказку, когда кнопка «+ Сервис» пропадёт. */}
            <p className={s.note} aria-live="polite">
              {addProtocol || level === 'high' ? ''
                : level === 'low' ? 'На Low — не больше одного TCP и одного HTTP.' : 'Не больше 8 сервисов.'}
            </p>
            {addProtocol && (
              <button type="button" className={ui.button}
                onClick={() => services.append({ protocol: addProtocol, port: '' as unknown as number, banner: '' }, { focusName: `services.${services.fields.length}.protocol` })}>
                + Сервис
              </button>
            )}
          </Panel>

          {hasCredentials(level) && (
            <Panel title="Учётные данные-приманки">
              <p className={s.note}>Ненастоящие логины и пароли, которые «подойдут» атакующему. Не больше 8.</p>
              {preservedUsernames.length > 0 && (
                <>
                  <p className={s.note}>Сохранённые логины: {preservedUsernames.join(', ')}. Пароли остаются на сервере.</p>
                  <label>
                    <input type="checkbox" checked={credentialsMode === 'replace'} onChange={(e) => {
                      form.setValue('credentials_mode', e.target.checked ? 'replace' : 'preserve', { shouldDirty: true });
                      creds.replace(e.target.checked ? preservedUsernames.map((username) => ({ username, password: '' })) : []);
                    }} /> Заменить учётные данные
                  </label>
                </>
              )}
              {errors.credentials?.root && <p className={s.error} role="alert">{errors.credentials.root.message}</p>}
              {errors.credentials?.message && <p className={s.error} role="alert">{errors.credentials.message}</p>}
              {credentialsMode === 'replace' && creds.fields.map((f, i) => (
                <div key={f.id} className={s.row} role="group" aria-label={`Учётка ${i + 1}`}>
                  <Field label="Логин" error={errors.credentials?.[i]?.username}>
                    {(a) => <input {...a} autoComplete="off" {...form.register(`credentials.${i}.username`)} />}
                  </Field>
                  <Field label="Пароль" error={errors.credentials?.[i]?.password}>
                    {(a) => <input {...a} autoComplete="off" {...form.register(`credentials.${i}.password`)} />}
                  </Field>
                  <button type="button" className={s.remove} onClick={() => creds.remove(i)} aria-label={`Удалить учётку ${i + 1}`}>×</button>
                </div>
              ))}
              {credentialsMode === 'replace' && (
                <button type="button" className={ui.button} disabled={creds.fields.length >= 8}
                  onClick={() => creds.append({ username: '', password: '' })}>+ Учётка</button>
              )}
            </Panel>
          )}

          {hasHigh(level) && (
            <Panel title="Среда High (VM)">
              <div className={s.row}>
                <Field label="TTL, с" error={errors.high?.ttl_seconds}>
                  {(a) => <input {...a} inputMode="numeric" {...form.register('high.ttl_seconds')} />}
                </Field>
                <Field label="CPU" error={errors.high?.cpu}>
                  {(a) => <input {...a} inputMode="numeric" {...form.register('high.cpu')} />}
                </Field>
                <Field label="Память, МБ" error={errors.high?.memory_mb}>
                  {(a) => <input {...a} inputMode="numeric" {...form.register('high.memory_mb')} />}
                </Field>
              </div>
            </Panel>
          )}

          <Panel title="Логирование и канал">
            <div className={s.row}>
              <Field label="Лимит payload, байт" error={errors.logging?.max_payload_bytes}>
                {(a) => <input {...a} inputMode="numeric" {...form.register('logging.max_payload_bytes')} />}
              </Field>
              <Field label="Интервал отправки, мс" error={errors.channel?.flush_interval_ms}>
                {(a) => <input {...a} inputMode="numeric" {...form.register('channel.flush_interval_ms')} />}
              </Field>
              <Field label="Размер пачки" error={errors.channel?.batch_size}>
                {(a) => <input {...a} inputMode="numeric" {...form.register('channel.batch_size')} />}
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
              {conflict && <button type="button" className={ui.button} onClick={() => void reload()}>Перечитать профиль и сбросить мои изменения</button>}
              {reloadError && <span role="alert" className={s.error}>{reloadError}</span>}
            </div>
          )}
        </fieldset>
      </form>

      {id && revisions.data && (
        <Panel title="Ревизии">
          <ol className={s.revisions}>
            {[...revisions.data].sort((a, b) => b.revision - a.revision).map((r) => (
              <li key={r.revision}><b>r{r.revision}</b> · {formatTime(r.created_at)}</li>
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
