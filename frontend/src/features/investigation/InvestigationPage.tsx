import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { api, ApiError } from '../../shared/api/client';
import type { AlertRead, ArtifactRead, EventRef, HoneytokenRead, HoneytokenTrigger, LookupRead, Page } from '../../shared/api/investigation';
import { formatTime } from '../../shared/format/time';
import { sourceLabel } from '../../shared/format/source';
import { Empty, ErrorState, Loading, Panel, StatusBadge, ui, type Tone } from '../../shared/ui';
import { canEdit, useMe } from '../auth/auth';
import { describeEvent, eventLabel } from '../events/describe';
import { artifactErrorMessage, downloadFile, formatBytes, IocPeriod, toLocalInput } from './logic';
import s from './investigation.module.css';

const TIME = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

/**
 * Список с курсором (specs/frontend.md, «Расследование»): по 50, «Показать ещё»;
 * 422 INVALID_CURSOR — перечитать с первой страницы.
 */
function usePaged<T>(key: readonly unknown[], path: string) {
  const qc = useQueryClient();
  const q = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams({ limit: '50' });
      if (pageParam) p.set('cursor', pageParam);
      return api<Page<T>>(`${path}${path.includes('?') ? '&' : '?'}${p}`);
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const more = async () => {
    const r = await q.fetchNextPage();
    if (r.error instanceof ApiError && r.error.code === 'INVALID_CURSOR') {
      qc.removeQueries({ queryKey: key, exact: true });
      void q.refetch();
    }
  };
  return { q, items: q.data?.pages.flatMap((p) => p.items) ?? [], more };
}

function MoreButton({ q, more }: { q: { hasNextPage: boolean; isFetchingNextPage: boolean }; more: () => Promise<void> }) {
  if (!q.hasNextPage) return null;
  return (
    <p className={s.more}>
      <button className={ui.button} disabled={q.isFetchingNextPage} onClick={() => void more()}>
        {q.isFetchingNextPage ? 'Загружаем…' : 'Показать ещё'}
      </button>
    </p>
  );
}

function ListState<T>({ q, items, what, empty, children }: {
  q: { isPending: boolean; isError: boolean; error: Error | null; refetch: () => unknown };
  items: T[]; what: string; empty: string; children: ReactNode;
}) {
  if (q.isPending) return <Loading what={what} />;
  if (q.isError && !items.length) return <ErrorState message={q.error?.message ?? 'Не удалось загрузить'} onRetry={() => void q.refetch()} />;
  if (!items.length) return <Empty>{empty}</Empty>;
  return <>{children}</>;
}

/** Экран «Расследование» (F16–F18): алерты, медовые токены, артефакты, экспорт IoC. */
export function InvestigationPage() {
  return (
    <>
      <h1>Расследование</h1>
      <AlertsPanel />
      <TokensPanel />
      <ArtifactsPanel />
      <IocExport />
    </>
  );
}

/**
 * Основание вывода (F17): исходные события по ссылкам через lookup.
 * Недоступные ссылки — «событие недоступно»; при усечении — «первые N из M».
 */
function Basis({ refs, truncated, total }: { refs: EventRef[]; truncated: boolean; total?: number }) {
  const unique = refs.filter((r, i) => refs.findIndex((x) => x.agent_id === r.agent_id && x.event_id === r.event_id) === i).slice(0, 100);
  const q = useQuery({
    queryKey: ['lookup', unique],
    queryFn: () => api<LookupRead>('/api/v1/events/lookup', { method: 'POST', body: JSON.stringify({ refs: unique }) }),
    enabled: unique.length > 0,
  });
  if (!unique.length) return <p className={s.note}>Ссылок на события нет.</p>;
  if (q.isPending) return <Loading what="основание" />;
  if (q.isError) return <ErrorState message={q.error.message} onRetry={() => void q.refetch()} />;
  return (
    <div className={s.basis}>
      {truncated && <p className={s.note}>Показаны первые {unique.length} из {total ?? 'многих'} событий.</p>}
      <ul className={s.events}>
        {q.data.items.map((e) => (
          <li key={`${e.agent_id}:${e.event_id}`}>
            <span className={s.time}>{formatTime(e.occurred_at)}</span>{' '}
            <span>{eventLabel(e.type)}</span>{' '}
            <span className="mono">{sourceLabel(e)}</span>{' '}
            {/* Только текст: данные атакующего не становятся разметкой (S1). */}
            <span className="mono">{describeEvent(e)}</span>
          </li>
        ))}
        {q.data.unavailable.map((r) => (
          <li key={`${r.agent_id}:${r.event_id}`} className={s.unavailable}>событие недоступно</li>
        ))}
      </ul>
    </div>
  );
}

function Toggle({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className={`${ui.button} ${s.small}`} aria-expanded={open} onClick={() => setOpen((o) => !o)}>{label}</button>
      {open && <div className={s.expand}>{children}</div>}
    </>
  );
}

function AlertsPanel() {
  const { q, items, more } = usePaged<AlertRead>(['alerts'], '/api/v1/alerts');
  return (
    <Panel title="Алерты">
      <p className={s.note}>Только чтение: правило «перебор паролей» (auth.bruteforce), открытые алерты.</p>
      <ListState q={q} items={items} what="алерты" empty="Алертов нет.">
        <ul className={s.cards}>
          {items.map((a) => (
            <li key={a.id} className={s.card}>
              <div className={s.cardHead}>
                <StatusBadge tone="bad">высокая важность</StatusBadge>
                {/* Заголовок формирует сервер — показываем текстом. */}
                <strong>{a.title}</strong>
              </div>
              <p className={s.meta}>
                Источник <span className="mono">{a.source_ip}</span> · окно {TIME.format(new Date(a.window.from))}–{TIME.format(new Date(a.window.to))}
                {' '}· событий: {a.count} · {formatTime(a.created_at)}
              </p>
              <Toggle label="Основание"><Basis refs={a.event_refs} truncated={a.event_refs_truncated} total={a.count} /></Toggle>
            </li>
          ))}
        </ul>
        <MoreButton q={q} more={more} />
      </ListState>
    </Panel>
  );
}

function TokensPanel() {
  const { q, items, more } = usePaged<HoneytokenRead>(['honeytokens'], '/api/v1/honeytokens');
  return (
    <Panel title="Медовые токены">
      <p className={s.note}>Ссылки-приманки (URL). Значение токена не показывается.</p>
      <ListState q={q} items={items} what="токены" empty="Медовых токенов нет.">
        <ul className={s.cards}>
          {items.map((t) => (
            <li key={t.id} className={s.card}>
              <div className={s.cardHead}>
                <StatusBadge tone={t.trigger_count ? 'bad' : 'idle'}>{t.trigger_count ? `сработал: ${t.trigger_count}` : 'не срабатывал'}</StatusBadge>
                <span className="mono">{t.label}</span>
              </div>
              <p className={s.meta}>
                Создан {formatTime(t.created_at)}
                {t.last_triggered_at && <> · последнее срабатывание {formatTime(t.last_triggered_at)}</>}
              </p>
              {t.trigger_count > 0 && <Toggle label="Срабатывания"><Triggers tokenId={t.id} /></Toggle>}
            </li>
          ))}
        </ul>
        <MoreButton q={q} more={more} />
      </ListState>
    </Panel>
  );
}

function Triggers({ tokenId }: { tokenId: string }) {
  const { q, items, more } = usePaged<HoneytokenTrigger>(['honeytoken-triggers', tokenId],
    `/api/v1/honeytokens/${encodeURIComponent(tokenId)}/triggers`);
  return (
    <ListState q={q} items={items} what="срабатывания" empty="Срабатываний нет.">
      <ul className={s.cards}>
        {items.map((t) => (
          <li key={`${t.event_refs[0]?.agent_id}:${t.event_refs[0]?.event_id}`} className={s.card}>
            <p className={s.meta}>
              {formatTime(t.at)} · <span className="mono">{sourceLabel(t)}</span>
              {t.session_id ? ' · привязано к сессии' : ' · без сессии'}
            </p>
            <Toggle label="Основание"><Basis refs={t.event_refs} truncated={false} /></Toggle>
          </li>
        ))}
      </ul>
      <MoreButton q={q} more={more} />
    </ListState>
  );
}

const ARTIFACT_STATE: Record<ArtifactRead['state'], { label: string; tone: Tone }> = {
  available: { label: 'доступен', tone: 'ok' },
  pending: { label: 'проверяется', tone: 'warn' },
  failed: { label: 'не получен', tone: 'bad' },
  expired: { label: 'срок хранения истёк', tone: 'idle' },
};

function ArtifactsPanel() {
  const { q, items, more } = usePaged<ArtifactRead>(['artifacts'], '/api/v1/artifacts');
  const download = canEdit(useMe()?.user.role);
  return (
    <Panel title="Артефакты">
      <p className={s.note}>Файлы, которые атакующие пытались загрузить. Без предпросмотра — только скачивание{download ? '' : ' (оператору и администратору)'}.</p>
      <ListState q={q} items={items} what="артефакты" empty="Артефактов нет.">
        <div className={s.scroll} tabIndex={0} role="region" aria-label="Таблица артефактов">
          <table className={ui.table}>
            <thead><tr><th>Файл</th><th>Состояние</th><th>Размер</th><th>SHA-256</th><th>Получен</th>{download && <th>Действия</th>}</tr></thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id}>
                  <td>
                    {/* Имя и адрес — данные атакующего: только текст, не ссылка (S1, S3). */}
                    <span className="mono">{a.name}</span>
                    {a.source_url && <div className={`${s.meta} mono`}>{a.source_url}</div>}
                    {a.error && <div className={s.meta}>{a.error}</div>}
                  </td>
                  <td><StatusBadge tone={ARTIFACT_STATE[a.state].tone}>{ARTIFACT_STATE[a.state].label}</StatusBadge></td>
                  <td>{formatBytes(a.size_bytes)}</td>
                  <td className={`mono ${s.hash}`}>{a.sha256 ?? '—'}</td>
                  <td>{formatTime(a.observed_at)}</td>
                  {download && <td>{a.state === 'available' ? <DownloadButton artifact={a} /> : '—'}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <MoreButton q={q} more={more} />
      </ListState>
    </Panel>
  );
}

function DownloadButton({ artifact }: { artifact: ArtifactRead }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true); setError(null);
    try {
      await downloadFile(`/api/v1/artifacts/${encodeURIComponent(artifact.id)}/content`, `${artifact.id}.bin`);
    } catch (err) {
      setError(artifactErrorMessage(err));
      // Состояние на сервере могло измениться — перечитываем список.
      void qc.invalidateQueries({ queryKey: ['artifacts'] });
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button className={`${ui.button} ${s.small}`} disabled={busy} onClick={() => void run()}
        aria-label={`Скачать ${artifact.name}`}>{busy ? 'Скачиваем…' : 'Скачать'}</button>
      {error && <div role="alert" className={s.error}>{error}</div>}
    </>
  );
}

/** Экспорт IoC (F18): обе даты обязательны, по умолчанию — последние 24 часа. */
function IocExport() {
  const [initial] = useState(() => IocPeriod.lastDay());
  const [from, setFrom] = useState(() => toLocalInput(initial.from));
  const [to, setTo] = useState(() => toLocalInput(initial.to));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const period = IocPeriod.fromInputs(from, to);
  const invalid = period.validate();

  const run = async () => {
    if (invalid) return;
    setBusy(true); setMessage(null);
    try {
      const name = await downloadFile(`/api/v1/iocs.csv?${period.toQuery()}`, 'honeyforge-iocs.csv');
      setMessage({ tone: 'ok', text: `Файл ${name} сохранён.` });
    } catch (err) {
      setMessage({ tone: 'bad', text: err instanceof Error ? err.message : 'Не удалось выгрузить.' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Экспорт IoC">
      <p className={s.note}>CSV с индикаторами (IPv4, IPv6, URL, SHA-256) за период до {31} дней. Время — локальное.</p>
      <div className={s.form}>
        <label>С <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} required /></label>
        <label>По <input type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} required /></label>
        <button className={ui.button} disabled={busy || !!invalid} onClick={() => void run()}>{busy ? 'Выгружаем…' : 'Скачать CSV'}</button>
      </div>
      {invalid && <p className={s.error} role="alert">{invalid}</p>}
      {message && <p role={message.tone === 'bad' ? 'alert' : 'status'} className={message.tone === 'bad' ? s.error : s.note}>{message.text}</p>}
    </Panel>
  );
}
