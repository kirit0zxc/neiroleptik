import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../shared/api/client';
import type { Profile, ProfileConfigInput, Revision } from '../../shared/api/types';

/** Профили и их ревизии по контракту `openapi.json` (ProfileRead/RevisionRead). */

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

/** История ревизий профиля (`GET /profiles/{id}/revisions`). */
export function useRevisions(id: string | undefined) {
  return useQuery({
    queryKey: [...KEY, id, 'revisions'],
    queryFn: () => api<Revision[]>(`/api/v1/profiles/${encodeURIComponent(id!)}/revisions`),
    enabled: !!id,
  });
}

export interface ProfileSavePayload {
  name: string;
  description: string;
  config: ProfileConfigInput;
}

/**
 * Создать профиль (POST) или сохранить новую ревизию (PATCH). Обновление
 * требует `expected_revision` — ожидаемую текущую ревизию, по которой сервер
 * обнаруживает конкурентное изменение (409).
 */
export function useSaveProfile(id: string | undefined, expectedRevision: number | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProfileSavePayload) => (id
      ? api<Profile>(`/api/v1/profiles/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ expected_revision: expectedRevision, ...payload }),
      })
      : api<Profile>('/api/v1/profiles', { method: 'POST', body: JSON.stringify(payload) })),
    onSuccess: (saved) => {
      qc.setQueryData([...KEY, saved.id], saved);
      void qc.invalidateQueries({ queryKey: KEY, exact: true });
      void qc.invalidateQueries({ queryKey: [...KEY, saved.id, 'revisions'] });
    },
  });
}
