import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { api, ApiError } from '../../shared/api/client';
import { isFinal, type Job } from './commands';

/** Сбой, после которого стоит спрашивать снова: нет связи или сервер не ответил. */
export const transient = (e: unknown) => e instanceof ApiError && (e.status === 0 || e.status >= 500);

/**
 * Ход задания (specs, «Команды ловушки»): опрос раз в секунду до итога,
 * затем перечитать парк — состояние ловушки изменилось. Временный сбой
 * опроса — не итог: опрос продолжается, а `polling.lost` показывает «нет
 * связи». Отказ сервера (например, 404 или 403) останавливает опрос с
 * понятной причиной в `polling.failure`.
 */
export function useJob(jobId: string | null) {
  const qc = useQueryClient();
  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api<Job>(`/api/v1/jobs/${encodeURIComponent(jobId!)}`),
    enabled: !!jobId,
    retry: false,
    refetchInterval: (q) => {
      if (q.state.data && isFinal(q.state.data.state)) return false;
      if (q.state.error && !transient(q.state.error)) return false;
      return 1000;
    },
    refetchIntervalInBackground: true,
  });
  const done = !!job.data && isFinal(job.data.state);
  const refused = !!jobId && !done && job.isError && !transient(job.error);
  const finished = done || refused;
  useEffect(() => {
    if (finished) void qc.invalidateQueries({ queryKey: ['deployments'] });
  }, [finished, qc]);
  return {
    job: job.data,
    finished,
    polling: {
      lost: !!jobId && !finished && job.isError,
      failure: refused ? `не удалось проверить задание: ${job.error instanceof ApiError ? job.error.message : 'ошибка'}` : null,
    },
  };
}
