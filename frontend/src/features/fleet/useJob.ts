import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { api } from '../../shared/api/client';
import { isFinal, type Job } from './commands';

/**
 * Ход задания (specs, «Команды ловушки»): опрос раз в секунду до итога,
 * затем перечитать парк — состояние ловушки изменилось.
 */
export function useJob(jobId: string | null) {
  const qc = useQueryClient();
  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api<Job>(`/api/v1/jobs/${encodeURIComponent(jobId!)}`),
    enabled: !!jobId,
    refetchInterval: (q) => (q.state.data && isFinal(q.state.data.state) ? false : 1000),
  });
  const finished = !!job.data && isFinal(job.data.state);
  useEffect(() => {
    if (finished) void qc.invalidateQueries({ queryKey: ['deployments'] });
  }, [finished, qc]);
  return { job: job.data, finished, error: job.error };
}
