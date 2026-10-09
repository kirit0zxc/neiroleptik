import { useMutation } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../shared/api/client';
import type { PreviewDeployment } from '../../shared/api/types';
import { StatusBadge, ui, WindowTitle } from '../../shared/ui';
import { availableCommands, COMMAND_LABEL, CONFIRM, jobLabel, type Command } from './commands';
import { useJob } from './useJob';
import s from './fleet.module.css';

/**
 * Команды одной ловушки (F6): кнопки по состоянию, подтверждение опасных,
 * ход задания до итога. Новый idempotency_key на каждое нажатие.
 */
export function DeploymentActions({ deployment }: { deployment: PreviewDeployment }) {
  // Команд и jobs пока нет в control OpenAPI: прямой рендер тоже безопасен.
  if (import.meta.env.VITE_API_MOCK !== '1') return null;
  return <PreviewActions deployment={deployment} />;
}

function PreviewActions({ deployment }: { deployment: PreviewDeployment }) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Command | null>(null);

  const send = useMutation({
    mutationFn: (command: Command) => api<{ job_id: string }>(
      `/api/v1/deployments/${encodeURIComponent(deployment.id)}/commands`,
      { method: 'POST', body: JSON.stringify({ command, idempotency_key: crypto.randomUUID() }) },
    ),
    onSuccess: (r) => setJobId(r.job_id),
  });

  const { job: current, finished } = useJob(jobId);

  const busy = send.isPending || (!!jobId && !finished);
  const commands = availableCommands(deployment);
  const run = (c: Command) => (CONFIRM[c] ? setConfirm(c) : send.mutate(c));

  return (
    <div className={s.actions}>
      {commands.map((c) => (
        <button key={c} className={`${ui.button} ${s.small}`} disabled={busy} onClick={() => run(c)}>
          {COMMAND_LABEL[c]}
        </button>
      ))}
      {current && (
        <span role="status" aria-label={`Задание ${deployment.id}`}>
          <StatusBadge tone={jobLabel(current).tone}>{COMMAND_LABEL[current.command]}: {jobLabel(current).label}</StatusBadge>
        </span>
      )}
      {send.error && (
        <span role="alert" className={s.err}>
          {send.error instanceof ApiError ? send.error.message : 'Команда не отправлена'}
        </span>
      )}
      {confirm && (
        <ConfirmDialog
          title={`${COMMAND_LABEL[confirm]} — ${deployment.id}?`}
          text={CONFIRM[confirm]!}
          onCancel={() => setConfirm(null)}
          onConfirm={() => { send.mutate(confirm); setConfirm(null); }}
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
