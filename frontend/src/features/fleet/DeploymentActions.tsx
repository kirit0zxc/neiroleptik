import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../../shared/api/client';
import type { Deployment } from '../../shared/api/types';
import { StatusBadge, ui, WindowTitle, type Tone } from '../../shared/ui';
import { availableCommands, COMMAND_LABEL, commandRejectText, CONFIRM, failureText, jobLabel, type Command } from './commands';
import { useOperation } from './operations';
import { useYumi } from '../yumi/yumi';
import { transient, useJob } from './useJob';
import s from './fleet.module.css';

/**
 * Команды одной ловушки (F6): кнопки по состоянию, подтверждение опасных,
 * ход задания до итога, понятные отказы. Сама операция (действие, ключ,
 * задание) живёт в хранилище Operations — строка только показывает её.
 * Кнопки видит только operator/admin — это решает парк.
 */
export function DeploymentActions({ deployment }: { deployment: Deployment }) {
  const [op, ops] = useOperation(deployment.id);
  const { action, jobId, sending, error } = op;
  const [confirm, setConfirm] = useState<Command | null>(null);
  const status = useRef<HTMLSpanElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  // Перевести фокус к ходу задания, когда модальный диалог уже закрыт: пока он
  // открыт, всё вокруг недоступно для фокуса.
  const [focusStatus, setFocusStatus] = useState(false);
  useEffect(() => {
    if (!focusStatus || confirm) return;
    status.current?.focus();
    setFocusStatus(false);
  }, [focusStatus, confirm]);

  const { job, finished, polling } = useJob(jobId);
  // Задание завершилось — следующее нажатие будет новым действием с новым ключом.
  useEffect(() => { if (finished) ops.finish(deployment.id); }, [finished, ops, deployment.id]);
  // Итог задания — Юми (раздел 9); один раз на задание. Отметка хранится в
  // Operations, а не в строке: возврат на страницу итог не повторит.
  const yumi = useYumi();
  useEffect(() => {
    if (!yumi || !job || !finished || !ops.reportOnce(job.job_id)) return;
    if (job.state === 'succeeded') yumi.handle('command:done', { command: job.command });
    else if (job.state === 'expired') yumi.handle('command:expired', { command: job.command });
    else if (job.state === 'failed') yumi.handle('command:failed', { command: job.command, reason: failureText(job) ?? '' });
  }, [yumi, job, finished, ops]);

  // Неотправленная команда (нет связи или результат неизвестен) ждёт
  // «Повторить»: другие команды до этого неактивны, иначе за возможно
  // созданным заданием никто не следил бы.
  const unsent = transient(error) && !!action;
  const busy = sending || (!!jobId && !finished) || unsent;
  const start = (command: Command) => {
    ops.begin(deployment.id, command, deployment.generation);
    // Нажатая кнопка сейчас станет неактивной — фокус переходит к ходу задания.
    setFocusStatus(true);
  };
  const ask = (c: Command, from: HTMLButtonElement) => {
    opener.current = from;
    if (CONFIRM[c]) setConfirm(c);
    else start(c);
  };

  const commands = availableCommands(deployment);
  const stale = error instanceof ApiError && error.code === 'STALE_GENERATION';
  // После STALE_GENERATION повторяем новым действием, когда парк перечитан
  // (поколение сменилось) и команда всё ещё доступна.
  const reread = stale && !!action && deployment.generation !== action.generation;
  const staleRetry = reread && commands.includes(action!.command) && !busy ? action!.command : null;

  // Одна постоянная живая область: меняется только текст — читалки объявляют надёжно.
  let progress: { tone: Tone; text: string } | null = null;
  if (polling.failure) progress = { tone: 'bad', text: polling.failure };
  else if (polling.lost) progress = { tone: 'warn', text: 'нет связи — проверяем снова' };
  if (job && !polling.failure) {
    const l = jobLabel(job);
    progress = { tone: polling.lost ? 'warn' : l.tone, text: `${COMMAND_LABEL[job.command]}: ${polling.lost ? 'нет связи — проверяем снова' : l.label}` };
  }

  return (
    <div className={s.actions}>
      {commands.map((c) => (
        <button key={c} className={`${ui.button} ${s.small}`} disabled={busy} onClick={(e) => ask(c, e.currentTarget)}>
          {COMMAND_LABEL[c]}
        </button>
      ))}
      <span role="status" ref={status} tabIndex={-1}>
        {progress && <StatusBadge tone={progress.tone}>{progress.text}</StatusBadge>}
      </span>
      {!!error && !sending && (
        <span role="alert" className={s.err}>
          {commandRejectText(error)}
          {unsent && (
            <>
              {' Повтор безопасен: вторая команда не создастся.'}
              {' '}<button className={`${ui.button} ${s.small}`} onClick={() => { ops.retry(deployment.id); setFocusStatus(true); }}>Повторить</button>
              {/* Выход без перезагрузки, если сбой не проходит; парк покажет, дошла ли команда. */}
              {' '}<button className={`${ui.button} ${s.small}`} onClick={() => ops.cancel(deployment.id)}>Отменить</button>
            </>
          )}
          {staleRetry && (
            <> Повторить? <button className={`${ui.button} ${s.small}`} onClick={(e) => ask(staleRetry, e.currentTarget)}>Повторить</button></>
          )}
          {reread && !staleRetry && <> Команда больше недоступна в текущем состоянии.</>}
        </span>
      )}
      {confirm && (
        <ConfirmDialog
          title={`${COMMAND_LABEL[confirm]} — ${deployment.id}?`}
          text={CONFIRM[confirm]!}
          onCancel={() => { setConfirm(null); opener.current?.focus(); }}
          onConfirm={() => { const c = confirm; setConfirm(null); start(c); }}
        />
      )}
    </div>
  );
}

/**
 * Модальное подтверждение на нативном <dialog>: фокус и Esc — из коробки.
 * `error` — сбой действия внутри окна (повтор — снова «Подтвердить»);
 * `busy` — запрос идёт, кнопки заблокированы.
 */
export function ConfirmDialog({ title, text, onCancel, onConfirm, error, busy = false }: {
  title: string; text: string; onCancel: () => void; onConfirm: () => void; error?: string | null; busy?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d || d.open) return;
    // Модально: затемнение фона, фокус внутри, Esc закрывает. Без showModal
    // (старая тестовая среда) — просто показываем.
    if (typeof d.showModal === 'function') d.showModal();
    else d.setAttribute('open', '');
  }, []);
  return (
    <dialog ref={ref} className={`win ${s.dialog}`} aria-labelledby="confirm-title" onCancel={(e) => { e.preventDefault(); if (!busy) onCancel(); }}>
      <WindowTitle title={title} id="confirm-title" />
      <p>{text}</p>
      {error && <p role="alert" className={s.dialogError}>{error}</p>}
      <div className={s.dialogActions}>
        <button className={ui.button} onClick={onCancel} disabled={busy} autoFocus>Отмена</button>
        <button className={`${ui.button} ${s.danger}`} onClick={onConfirm} disabled={busy}>{busy ? 'Выполняем…' : error ? 'Повторить' : 'Подтвердить'}</button>
      </div>
    </dialog>
  );
}
