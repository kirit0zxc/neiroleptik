import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../shared/api/client';
import type { Deployment } from '../../shared/api/types';
import { StatusBadge, ui, WindowTitle, type Tone } from '../../shared/ui';
import { availableCommands, COMMAND_LABEL, commandRejectText, CONFIRM, jobLabel, type Command } from './commands';
import { transient, useJob } from './useJob';
import s from './fleet.module.css';

/**
 * Действие пользователя: команда, ключ идемпотентности и поколение, по
 * которому он видел ловушку. Ключ создаётся один раз на действие и
 * повторяется при повторной отправке после сбоя (specs, «Команды ловушки»).
 */
interface Action { command: Command; key: string; generation: number }

/**
 * Команды одной ловушки (F6): кнопки по состоянию, подтверждение опасных,
 * ход задания до итога, понятные отказы. Кнопки видит только operator/admin —
 * это решает парк.
 */
export function DeploymentActions({ deployment }: { deployment: Deployment }) {
  const qc = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Command | null>(null);
  const status = useRef<HTMLSpanElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);

  const send = useMutation({
    mutationFn: (a: Action) => api<{ job_id: string }>(
      `/api/v1/deployments/${encodeURIComponent(deployment.id)}/commands`,
      { method: 'POST', body: JSON.stringify({ command: a.command, idempotency_key: a.key, expected_generation: a.generation }) },
    ),
    onSuccess: (r) => setJobId(r.job_id),
    // Отказ центра (409) — данные устарели или изменились: перечитываем парк.
    onError: (e) => { if (e instanceof ApiError && e.status === 409) void qc.invalidateQueries({ queryKey: ['deployments'] }); },
  });

  const { job, finished, polling } = useJob(jobId);
  // Задание завершилось — следующее нажатие будет новым действием с новым ключом.
  useEffect(() => { if (finished) setAction(null); }, [finished]);

  // Неотправленная команда (нет связи) ждёт «Повторить»: другие команды до
  // этого неактивны, иначе за возможно созданным заданием никто не следил бы.
  const unsent = transient(send.error) && !!action;
  const busy = send.isPending || (!!jobId && !finished) || unsent;
  const start = (command: Command) => {
    const next = { command, key: crypto.randomUUID(), generation: deployment.generation };
    setAction(next);
    setJobId(null);
    send.mutate(next);
    // Нажатая кнопка сейчас станет неактивной — фокус переходит к ходу задания.
    status.current?.focus();
  };
  const ask = (c: Command, from: HTMLButtonElement) => {
    opener.current = from;
    if (CONFIRM[c]) setConfirm(c);
    else start(c);
  };

  const commands = availableCommands(deployment);
  const stale = send.error instanceof ApiError && send.error.code === 'STALE_GENERATION';
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
      {send.error && !send.isPending && (
        <span role="alert" className={s.err}>
          {commandRejectText(send.error)}
          {unsent && (
            <> <button className={`${ui.button} ${s.small}`} onClick={() => send.mutate(action!)}>Повторить</button></>
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

/** Модальное подтверждение на нативном <dialog>: фокус и Esc — из коробки. */
function ConfirmDialog({ title, text, onCancel, onConfirm }: {
  title: string; text: string; onCancel: () => void; onConfirm: () => void;
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
    <dialog ref={ref} className={`win ${s.dialog}`} aria-labelledby="confirm-title" onCancel={(e) => { e.preventDefault(); onCancel(); }}>
      <WindowTitle title={title} id="confirm-title" />
      <p>{text}</p>
      <div className={s.dialogActions}>
        <button className={ui.button} onClick={onCancel} autoFocus>Отмена</button>
        <button className={`${ui.button} ${s.danger}`} onClick={onConfirm}>Подтвердить</button>
      </div>
    </dialog>
  );
}
