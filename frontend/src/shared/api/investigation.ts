/**
 * Типы расследования — по общему контракту `contracts/openapi.json`
 * (PR #35; семантика — specs/investigation-handoff.md). Только утверждённые
 * поля: ничего из proposal сюда не переносится.
 */
import type { HfEvent } from './types';

export interface EventRef { agent_id: string; event_id: string }
export interface Endpoint { ip: string; port: number }

export interface Page<T> { items: T[]; next_cursor: string | null }

export type ArtifactState = 'pending' | 'available' | 'failed' | 'expired';

export interface ArtifactRead {
  id: string;
  session_id: string | null;
  deployment_id: string;
  state: ArtifactState;
  /** Имя из данных атакующего — только текст. */
  name: string;
  /** Не делается ссылкой (S3). */
  source_url: string | null;
  size_bytes: number | null;
  sha256: string | null;
  mime: string | null;
  observed_at: string;
  expires_at: string | null;
  error: string | null;
  event_refs: EventRef[];
  event_refs_truncated: boolean;
}

export interface HoneytokenRead {
  id: string;
  deployment_id: string;
  kind: 'url';
  label: string;
  created_at: string;
  last_triggered_at: string | null;
  trigger_count: number;
}

export interface HoneytokenTrigger {
  at: string;
  source: Endpoint | null;
  session_id: string | null;
  /** Ровно одно событие token.triggered. */
  event_refs: EventRef[];
}

export interface AlertRead {
  id: string;
  rule: 'auth.bruteforce';
  rule_version: number;
  severity: 'high';
  state: 'open';
  title: string;
  window: { from: string; to: string };
  count: number;
  deployment_id: string;
  session_id: string | null;
  source_ip: string;
  created_at: string;
  updated_at: string;
  event_refs: EventRef[];
  event_refs_truncated: boolean;
}

export interface LookupRead { items: HfEvent[]; unavailable: EventRef[] }
