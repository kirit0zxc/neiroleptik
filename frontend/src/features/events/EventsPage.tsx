import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router';
import { api, ApiError } from '../../shared/api/client';
import { EVENT_TYPES, eventKey, type Deployment, type HfEvent } from '../../shared/api/types';
import { formatTime } from '../../shared/format/time';
import { useStreamEvents, useStreamResync } from '../../shared/stream/stream';
import { Empty, ErrorState, Loading, Panel, ui } from '../../shared/ui';
import { describeEvent, eventLabel } from './describe';
import { FeedBuffer } from './FeedBuffer';
import { filtersFromSearch, filtersToSearch, matches, toQuery, validateFilters, type EventFilters } from './filters';
import s from './events.module.css';

interface EventsResponse { items: HfEvent[]; next_cursor: string | null }

/**
 * Лента событий (F10–F13): фильтры в адресе страницы, страницы по курсору,
 * живые события сверху, пауза. Данные атакующего — только текстом (S1).
 */
export function EventsPage() {
  const [search, setSearch] = useSearchParams();
  const filters = useMemo(() => filtersFromSearch(search), [search]);
  const key = filtersToSearch(filters).toString();
  const qc = useQueryClient();

  const pages = useInfiniteQuery({
    queryKey: ['events', key],
    queryFn: ({ pageParam }) => api<EventsResponse>(`/api/v1/events?${toQuery(filters, pageParam)}`),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // При возврате на ленту историю перечитываем: пока экран был закрыт, в
    // ней могли появиться события, которых нет в прошлой выборке.
    staleTime: 0,
    refetchOnMount: 'always',
  });

  const buffer = useRef(new FeedBuffer());
  const [, rerender] = useState(0);
  const [resyncing, setResyncing] = useState(false);
  const [resyncError, setResyncError] = useState<string | null>(null);
  const refresh = () => rerender((n) => n + 1);

  // Новая выборка не наследует live и pending предыдущих фильтров.
  useEffect(() => { buffer.current.loadPage([], false); }, [key]);

  // Собрать окно: страницы REST + живые события, пришедшие после загрузки.
  // Паузу не трогаем: на паузе живые события остаются в pending, а не
  // показываются при догрузке истории («Показать ещё»).
  useEffect(() => {
    if (!pages.data) return;
    const b = buffer.current;
    b.replaceHistory(pages.data.pages.flatMap((p) => p.items));
    refresh();
  }, [pages.data]);

  // Старый запрос/курсор снимка не переиспользуем после hello или overflow.
  // StreamClient ждёт завершения этого Promise и буферизует новый live.
  const reloadHistory = useCallback(async () => {
    setResyncing(true);
    setResyncError(null);
    try {
      const queryKey = ['events', key];
      await qc.cancelQueries({ queryKey, exact: true });
      qc.setQueryData<InfiniteData<EventsResponse>>(queryKey, (old) => old
        ? { pages: old.pages.slice(0, 1), pageParams: [null] }
        : old);
      buffer.current.loadPage([], false);
      await pages.refetch({ throwOnError: true });
    } catch (err) {
      setResyncError(err instanceof Error ? err.message : 'Не удалось обновить историю');
      throw err;
    } finally {
      setResyncing(false);
      rerender((n) => n + 1);
    }
  }, [qc, key, pages.refetch]);
  useStreamResync(reloadHistory);

  const recoverOverflow = () => { void reloadHistory().catch(() => {}); };

  // 422 на следующей странице: курсор снимка больше не принимается — перечитать с
  // начала, но автоматически один раз. Повторный 422 — ошибка: неверные фильтры
  // перечитыванием не исправить. Новая выборка или успешная страница снимают отметку.
  const [restarted, setRestarted] = useState(false);
  const autoReset = useRef(false);
  useEffect(() => { setRestarted(false); autoReset.current = false; }, [key]);
  const loadMore = async () => {
    setRestarted(false);
    const r = await pages.fetchNextPage();
    if (!(r.error instanceof ApiError && r.error.status === 422)) {
      if (!r.error) autoReset.current = false;
      return;
    }
    // Повтор: остаётся ошибка запроса с кнопкой «Повторить».
    if (autoReset.current) return;
    autoReset.current = true;
    setRestarted(true);
    await reloadHistory().catch(() => {});
  };

  const onLive = useCallback((events: HfEvent[]) => {
    const fit = events.filter((e) => matches(e, filters));
    if (!fit.length) return;
    buffer.current.pushLive(fit);
    refresh();
  }, [filters]);
  useStreamEvents(onLive);

  const b = buffer.current;
  const apply = (next: EventFilters) => setSearch(filtersToSearch(next));

  return (
    <>
      <h1>События</h1>
      <FilterBar value={filters} onApply={apply} />
      <Panel title="Лента">
        <div className={s.toolbar}>
          <button className={ui.button} aria-pressed={b.isPaused} disabled={resyncing} onClick={() => {
            const resume = b.isPaused;
            b.setPaused(!b.isPaused);
            if (resume && b.overflow > 0) recoverOverflow();
            refresh();
          }}>
            {b.isPaused ? '▶ Продолжить' : '⏸ Пауза'}
          </button>
          {b.pending > 0 && (
            <button className={ui.button} disabled={resyncing} onClick={() => {
              if (b.overflow > 0) recoverOverflow();
              else b.flush();
              refresh();
            }}>
              {b.pending} новых — показать
            </button>
          )}
          <span className={s.count}>Показано: {b.events.length}</span>
        </div>
        {b.overflow > 0 && <p role="alert">Буфер паузы переполнен: {b.overflow} событий требуют повторной загрузки.{' '}
          <button className={ui.button} disabled={resyncing} onClick={recoverOverflow}>Обновить историю</button>
        </p>}
        {restarted && <p role="status">Список событий обновлён — загружен заново.</p>}
        {resyncError && <ErrorState message={resyncError} onRetry={recoverOverflow} />}
        {pages.isPending || resyncing ? <Loading what="события" />
          : pages.isError ? <ErrorState message={pages.error.message} onRetry={() => void pages.refetch()} />
          : !b.events.length ? <Empty>{key ? 'Под фильтры ничего не подходит.' : 'Событий пока нет — ловушки ждут гостей.'}</Empty>
          : (
            <table className={ui.table}>
              <thead><tr><th>Время</th><th>Тип</th><th>Ловушка</th><th>Источник</th><th>Подробности</th></tr></thead>
              <tbody>
                {b.events.map((e) => (
                  <tr key={eventKey(e)} data-testid="event-row">
                    <td>{formatTime(e.occurred_at)}</td>
                    <td>{eventLabel(e.type)}</td>
                    <td className="mono">{e.deployment_id}</td>
                    <td className="mono">{e.source.ip}:{e.source.port}</td>
                    {/* Только текст: данные атакующего не превращаются в разметку (S1). */}
                    <td className="mono">{describeEvent(e)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        {pages.hasNextPage && (
          <p className={s.more}>
            <button className={ui.button} disabled={pages.isFetchingNextPage || resyncing} onClick={() => void loadMore()}>
              {pages.isFetchingNextPage ? 'Загружаем…' : 'Показать ещё'}
            </button>
          </p>
        )}
      </Panel>
    </>
  );
}

/** Фильтры: применяются кнопкой, неверные значения подсвечиваются и не уходят в запрос. */
function FilterBar({ value, onApply }: { value: EventFilters; onApply: (f: EventFilters) => void }) {
  const [draft, setDraft] = useState<EventFilters>(value);
  const [errors, setErrors] = useState<ReturnType<typeof validateFilters>>({});
  const deps = useQuery({ queryKey: ['deployments'], queryFn: () => api<Deployment[]>('/api/v1/deployments') });

  // Фильтры поменялись снаружи (назад/вперёд в браузере) — обновить поля.
  const [shown, setShown] = useState(value);
  if (shown !== value) { setShown(value); setDraft(value); setErrors({}); }

  const set = (k: keyof EventFilters) => (e: { target: { value: string } }) =>
    setDraft((d) => ({ ...d, [k]: e.target.value || undefined }));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const errs = validateFilters(draft);
    setErrors(errs);
    if (!Object.keys(errs).length) onApply(draft);
  };

  return (
    <Panel title="Фильтры">
    <form className={s.filters} onSubmit={submit} aria-label="Фильтры событий">
      <label className={s.f}>Ловушка
        <select value={draft.deployment_id ?? ''} onChange={set('deployment_id')}>
          <option value="">все</option>
          {deps.data?.map((d) => <option key={d.id} value={d.id}>{d.id}</option>)}
        </select>
      </label>
      <label className={s.f}>Тип
        <select value={draft.type ?? ''} onChange={set('type')}>
          <option value="">все</option>
          {EVENT_TYPES.map((t) => <option key={t} value={t}>{eventLabel(t)}</option>)}
        </select>
      </label>
      <div className={s.f}>
        <label htmlFor="flt-ip">IP источника</label>
        <input id="flt-ip" value={draft.source_ip ?? ''} onChange={set('source_ip')} placeholder="192.0.2.1"
          aria-invalid={!!errors.source_ip} aria-describedby={errors.source_ip ? 'flt-ip-err' : undefined} />
        {errors.source_ip && <span id="flt-ip-err" className={s.err}>{errors.source_ip}</span>}
      </div>
      <div className={s.f}>
        <label htmlFor="flt-from">С</label>
        <input id="flt-from" type="datetime-local" value={draft.from ?? ''} onChange={set('from')}
          aria-invalid={!!errors.from} />
        {errors.from && <span className={s.err}>{errors.from}</span>}
      </div>
      <div className={s.f}>
        <label htmlFor="flt-to">До</label>
        <input id="flt-to" type="datetime-local" value={draft.to ?? ''} onChange={set('to')}
          aria-invalid={!!errors.to} aria-describedby={errors.to ? 'flt-to-err' : undefined} />
        {errors.to && <span id="flt-to-err" className={s.err}>{errors.to}</span>}
      </div>
      <div className={s.actions}>
        <button className={ui.button} type="submit">Применить</button>
        <button className={ui.button} type="button" onClick={() => { setDraft({}); setErrors({}); onApply({}); }}>Сбросить</button>
      </div>
    </form>
    </Panel>
  );
}
