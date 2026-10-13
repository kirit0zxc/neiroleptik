/**
 * Типы API HoneyForge по общему контракту `contracts/openapi.json`.
 *
 * Для auth, профилей, хостов, развёртываний и команд (задания — в
 * features/fleet/commands.ts) имена и форма полей точно повторяют контракт.
 * Поле `created_at` и прочие строки дат — ISO 8601 в UTC.
 *
 * Поток событий в контракте ещё не закреплён (участник 4): его типы — превью
 * и обслуживаются моками.
 */

// ─── Пользователь и сессия (AuthRead/UserRead) ───────────────

export type Role = 'admin' | 'operator' | 'viewer';

/** Учётная запись оператора (UserRead). */
export interface UserRead {
  id: string;
  username: string;
  role: Role;
}

/** Ответ входа и `GET /auth/me` (AuthRead): пользователь и CSRF-токен. */
export interface Auth {
  user: UserRead;
  /** CSRF-токен для изменяющих запросов (хранится только в памяти). */
  csrf_token: string;
}

// ─── Хосты (HostRead) ────────────────────────────────────────

export type HostStatus = 'online' | 'offline' | 'revoked';

export interface Host {
  id: string;
  name: string;
  /** Поддерживаемые хостом уровни взаимодействия (`low`/`medium`/`high`). */
  capabilities: string[];
  /** Последний контакт агента; null — ещё ни разу не выходил на связь. */
  last_seen: string | null;
  status: HostStatus;
  created_at: string;
}

// ─── Профили (ProfileRead / ProfileConfigPublic) ─────────────

export type InteractionLevel = 'low' | 'medium' | 'high';
export type Protocol = 'tcp' | 'http' | 'ssh';

/** Сервис-приманка в конфигурации профиля (ServiceConfig). */
export interface ServiceConfig {
  protocol: Protocol;
  port: number;
  banner: string;
}

/** Параметры логирования (LoggingConfig). */
export interface LoggingConfig {
  max_payload_bytes: number;
}

/** Параметры канала доставки событий (ChannelConfig). */
export interface ChannelConfig {
  flush_interval_ms: number;
  batch_size: number;
}

/** Параметры среды High — настоящая VM за SSH-шлюзом (HighConfig). */
export interface HighConfig {
  template: 'ubuntu-ssh-v1';
  ttl_seconds: number;
  cpu: number;
  memory_mb: number;
}

/**
 * Конфигурация профиля на запись (ProfileConfigInput): учётки-приманки
 * передаются полностью, пароли — только в запросе.
 */
export interface ProfileConfigInput {
  interaction_level: InteractionLevel;
  hostname?: string;
  services: ServiceConfig[];
  logging?: LoggingConfig;
  channel?: ChannelConfig;
  high?: HighConfig | null;
  credentials?: { username: string; password: string }[] | null;
}

/**
 * Конфигурация профиля на чтение (ProfileConfigPublic): вместо паролей —
 * только логины учёток, сервер их не отдаёт.
 */
export interface ProfileConfigPublic {
  interaction_level: InteractionLevel;
  hostname?: string;
  services: ServiceConfig[];
  logging?: LoggingConfig;
  channel?: ChannelConfig;
  high?: HighConfig | null;
  credential_usernames: string[];
}

/** Профиль (ProfileRead). Имя и описание — на верхнем уровне, не в config. */
export interface Profile {
  id: string;
  name: string;
  description: string;
  archived: boolean;
  revision: number;
  config: ProfileConfigPublic;
  created_at: string;
}

/** Ревизия профиля из истории (RevisionRead). */
export interface Revision {
  revision: number;
  config: ProfileConfigPublic;
  created_at: string;
}

// ─── Развёртывания (DeploymentRead/DeploymentCreate) ─────────

/** Фактические состояния центра; unknown означает потерю подтверждения агента. */
export type DeploymentState = 'planned' | 'deploying' | 'running' | 'stopping' | 'stopped' | 'failed' | 'unknown';

/** Развёртывание (DeploymentRead): запись намерения, не запущенный процесс. */
export interface Deployment {
  id: string;
  host_id: string;
  profile_id: string;
  revision: number;
  state: DeploymentState;
  /** Уровень назначенной ревизии — из ответа, не из текущей версии профиля. */
  interaction_level: InteractionLevel;
  desired_revision: number;
  applied_revision: number | null;
  /** Счётчик принятых команд; отправляется как expected_generation. */
  generation: number;
  instance_id: string | null;
  created_at: string;
}

/** Тело создания развёртывания (DeploymentCreate). */
export interface DeploymentCreate {
  host_id: string;
  profile_id: string;
  revision: number;
}

// ─── События потока (превью: в OpenAPI ещё нет) ──────────────

/** Словарь типов событий v1; неизвестный тип UI показывает как «неизвестное». */
export const EVENT_TYPES = [
  'connection.opened', 'auth.attempt', 'session.command', 'session.output',
  'http.request', 'tcp.request', 'artifact.observed', 'token.triggered', 'session.closed', 'runtime.state',
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

/**
 * Событие потока (event-v1.schema.json). Поток в контракте стадии 1 ещё не
 * закреплён (участник 4), поэтому тип опирается на схему событий, а не на
 * OpenAPI. Уникальность события — пара `(agent_id, event_id)`.
 */
export interface HfEvent {
  schema_version: 1;
  event_id: string;
  agent_id: string;
  deployment_id: string;
  instance_id: string | null;
  session_id: string | null;
  source_seq: number;
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

/** Ключ дедупликации события: пара агент+идентификатор (а не только event_id). */
export function eventKey(e: Pick<HfEvent, 'agent_id' | 'event_id'>): string {
  return `${e.agent_id}\u0000${e.event_id}`;
}
