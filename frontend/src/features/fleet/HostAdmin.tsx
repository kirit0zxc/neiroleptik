import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../../shared/api/client';
import type { Host } from '../../shared/api/types';
import { ui, WindowTitle } from '../../shared/ui';
import { ConfirmDialog } from './DeploymentActions';
import { canRevoke, ENROLL_COMMAND, EnrollmentTicket, issueEnrollment, minutesLabel, revokeHost } from './hosts';
import s from './fleet.module.css';

const HOUR_MINUTE = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });

// Текст — от центра: 403 бывает и из-за CSRF/Origin, не только из-за роли.
const message = (err: unknown) => (err instanceof Error && err.message ? err.message : 'Не удалось выполнить запрос');

/**
 * «Добавить хост»: выдаёт одноразовый регистрационный токен агента и
 * показывает его один раз (specs/frontend.md, «Регистрация агента»). Токен
 * живёт только в состоянии окна; после закрытия список хостов перечитывается.
 */
export function AddHostButton() {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const qc = useQueryClient();
  return (
    <>
      <button ref={opener} className={ui.button} onClick={() => setOpen(true)}>+ Добавить хост</button>
      {open && (
        <EnrollmentDialog onClose={() => {
          setOpen(false);
          // Агент мог успеть зарегистрироваться, пока окно было открыто.
          void qc.invalidateQueries({ queryKey: ['hosts'] });
          opener.current?.focus();
        }} />
      )}
    </>
  );
}

function EnrollmentDialog({ onClose }: { onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [ticket, setTicket] = useState<EnrollmentTicket | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<'ok' | 'fail' | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const tokenInput = useRef<HTMLInputElement>(null);
  /** Номер запроса: ответ на прежний запрос после закрытия или повтора не показывается. */
  const request = useRef(0);

  const issue = async () => {
    const n = ++request.current;
    setBusy(true); setError(null); setCopied(null); setTicket(null);
    try {
      const read = await issueEnrollment();
      if (n !== request.current) return;
      setTicket(new EnrollmentTicket(read)); setNow(Date.now());
    } catch (err) {
      if (n === request.current) setError(message(err));
    } finally {
      if (n === request.current) setBusy(false);
    }
  };

  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) {
      if (typeof d.showModal === 'function') d.showModal();
      else d.setAttribute('open', '');
    }
    void issue();
    // Закрытие окна делает поздний ответ недействительным.
    return () => { request.current++; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Обратный отсчёт: раз в 15 с для минут и точно в момент истечения.
  useEffect(() => {
    if (!ticket) return;
    const t = setInterval(() => setNow(Date.now()), 15_000);
    const end = setTimeout(() => setNow(Date.now()), Math.max(0, ticket.expiresAt - Date.now()) + 50);
    return () => { clearInterval(t); clearTimeout(end); };
  }, [ticket]);

  const copy = async () => {
    if (!ticket) return;
    try {
      await navigator.clipboard.writeText(ticket.token);
      setCopied('ok');
    } catch {
      // Буфер обмена недоступен (нет прав, не HTTPS) — выделяем для ручного копирования.
      tokenInput.current?.select();
      setCopied('fail');
    }
  };

  const expired = ticket?.expired(now) ?? false;
  return (
    <dialog ref={ref} className={`win ${s.dialog} ${s.dialogWide}`} aria-labelledby="enroll-title"
      onCancel={(e) => { e.preventDefault(); onClose(); }}>
      <WindowTitle title="Регистрация агента" id="enroll-title" />
      {busy && <p role="status">Выдаём токен…</p>}
      {error && <p role="alert" className={s.dialogError}>Токен не выдан: {error}</p>}
      {ticket && (
        <>
          <p>Одноразовый токен регистрации. Он показан один раз: после закрытия окна его не получить.</p>
          <div className={s.token}>
            <input ref={tokenInput} readOnly value={ticket.token} aria-label="Токен регистрации"
              onFocus={(e) => e.currentTarget.select()} />
            {/* Не блокируем: срок считается по часам этого компьютера, они могут спешить. */}
            <button className={ui.button} onClick={() => void copy()}>Скопировать</button>
          </div>
          <p className={s.note} role="status">
            {copied === 'ok' && 'Скопировано. '}
            {copied === 'fail' && 'Буфер обмена недоступен — токен выделен, скопируйте вручную. '}
            {expired
              ? 'По часам этого компьютера токен истёк — выдайте новый.'
              : `Действует до ${HOUR_MINUTE.format(ticket.expiresAt)}, осталось ${minutesLabel(ticket.minutesLeft(now))}.`}
          </p>
          <p className={s.note}>
            На хосте передайте токен через переменную окружения <code>HF_ENROLLMENT_TOKEN</code> и выполните
            команду; <code>--token-file</code> — куда агент сохранит свой постоянный токен, не этот.
          </p>
          <pre className={s.cmd}>{ENROLL_COMMAND}</pre>
          <p className={s.note}>
            Токен одноразовый: если ответ регистрации потерян, повтор с ним не сработает — выдайте новый.
            Новый токен не отменяет прежние: они действуют до своего срока.
            Подробно — infra/runtime/executor.md.
          </p>
        </>
      )}
      <div className={s.dialogActions}>
        <button className={ui.button} onClick={() => void issue()} disabled={busy}>Выдать новый токен</button>
        <button className={ui.button} onClick={onClose} autoFocus>Закрыть</button>
      </div>
    </dialog>
  );
}

/** «Отозвать» хост (только admin): агент теряет доступ к центру, отмены нет. */
export function RevokeHostButton({ host }: { host: Host }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const opener = useRef<HTMLButtonElement>(null);
  /** Строка хоста для фокуса после отзыва; пока модальное окно открыто, вне его фокус не встаёт. */
  const focusRow = useRef<HTMLTableRowElement | null>(null);
  const qc = useQueryClient();
  useEffect(() => {
    const row = focusRow.current;
    if (confirming || !row) return;
    focusRow.current = null;
    row.tabIndex = -1;
    row.focus();
  }, [confirming]);
  if (!canRevoke(host)) return null;

  const close = () => { setConfirming(false); setError(null); opener.current?.focus(); };
  const revoke = async () => {
    setBusy(true); setError(null);
    try {
      const updated = await revokeHost(host.id);
      // Кнопка сейчас исчезнет — фокус переведём на строку хоста, когда окно закроется.
      focusRow.current = opener.current?.closest('tr') ?? null;
      qc.setQueryData<Host[]>(['hosts'], (old) => old?.map((h) => (h.id === updated.id ? updated : h)));
      setConfirming(false);
      void qc.invalidateQueries({ queryKey: ['hosts'] });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Хоста уже нет — показываем актуальный список.
        setConfirming(false);
        void qc.invalidateQueries({ queryKey: ['hosts'] });
      } else {
        setError(message(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button ref={opener} className={`${ui.button} ${s.danger}`} onClick={() => setConfirming(true)}
        aria-label={`Отозвать хост ${host.name}`}>Отозвать</button>
      {confirming && (
        <ConfirmDialog
          title={`Отозвать хост ${host.name}?`}
          text="Агент потеряет доступ к центру. Действие не отменить: чтобы вернуть хост, агента нужно зарегистрировать заново."
          error={error} busy={busy}
          onCancel={close}
          onConfirm={() => void revoke()}
        />
      )}
    </>
  );
}
