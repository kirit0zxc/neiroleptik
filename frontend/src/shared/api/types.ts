/**
 * Черновые типы API — по ответам тимлида 09.10 (specs, раздел 4).
 * После утверждения OpenAPI в D1 этот файл заменяется сгенерированным
 * (`openapi-typescript`), имена полей остаются прежними.
 */

export type Role = 'admin' | 'operator' | 'viewer';

export interface Me {
  user_id: string;
  login: string;
  role: Role;
  /** CSRF-токен для изменяющих запросов (хранится только в памяти). */
  csrf_token: string;
}

export type HostStatus = 'online' | 'offline' | 'degraded';

export interface Host {
  host_id: string;
  agent_id: string;
  status: HostStatus;
  last_seen: string;
  backlog: number;
  disk_free_mb: number;
  /** Что умеет хост: docker, kvm (для High). */
  capabilities: string[];
}

export type InteractionLevel = 'low' | 'medium' | 'high';
export type DeploymentState = 'planned' | 'deploying' | 'running' | 'stopping' | 'stopped' | 'failed';

export interface Deployment {
  deployment_id: string;
  host_id: string;
  interaction_level: InteractionLevel;
  state: DeploymentState;
  profile_id: string;
  desired_revision: number;
  applied_revision: number | null;
}

/** Словарь типов событий v1; неизвестный тип UI показывает как «неизвестное». */
export const EVENT_TYPES = [
  'connection.opened', 'auth.attempt', 'session.command', 'session.output',
  'http.request', 'artifact.observed', 'token.triggered', 'session.closed', 'runtime.state',
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export interface HfEvent {
  event_id: string;
  agent_id: string;
  deployment_id: string;
  instance_id: string | null;
  session_id: string | null;
  occurred_at: string;
  /** Время приёма центром. */
  received_at: string;
  type: EventType | string;
  source: { ip: string; port: number };
  service: { protocol: string; port: number };
  data: Record<string, unknown>;
  artifact_ids: string[];
  profile_revision: number;
}
