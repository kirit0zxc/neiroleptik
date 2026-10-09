import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../shared/api/client';
import type { ProfileConfig } from './schema';

/** Профиль с историей ревизий (черновик контракта, specs F8–F9). */
export interface Profile {
  profile_id: string;
  revision: number;
  updated_at: string;
  revisions: { revision: number; created_at: string; created_by: string }[];
  config: ProfileConfig;
}

const KEY = ['profiles'] as const;

export function useProfiles() {
  return useQuery({ queryKey: KEY, queryFn: () => api<Profile[]>('/api/v1/profiles') });
}

export function useProfile(id: string | undefined) {
  return useQuery({
    queryKey: [...KEY, id],
    queryFn: () => api<Profile>(`/api/v1/profiles/${encodeURIComponent(id!)}`),
    enabled: !!id,
  });
}

/** Создать профиль или сохранить новую ревизию существующего (F9). */
export function useSaveProfile(id: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (config: ProfileConfig) => (id
      ? api<Profile>(`/api/v1/profiles/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ config }) })
      : api<Profile>('/api/v1/profiles', { method: 'POST', body: JSON.stringify({ config }) })),
    onSuccess: (saved) => {
      qc.setQueryData([...KEY, saved.profile_id], saved);
      void qc.invalidateQueries({ queryKey: KEY, exact: true });
    },
  });
}
