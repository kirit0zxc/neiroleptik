import { z } from 'zod';
import type { InteractionLevel, ProfileConfigInput, Protocol } from '../../shared/api/types';

/**
 * Форма профиля ловушки по контракту `openapi.json`
 * (ProfileCreate + ProfileConfigInput). Имя и описание — на верхнем уровне,
 * остальное — в `config`. Значения совпадают с ограничениями сервера, чтобы
 * форма отсекала неверный ввод до запроса.
 */

export const LEVELS = ['low', 'medium', 'high'] as const;
export const PROTOCOLS = ['tcp', 'http', 'ssh'] as const;
export type Level = InteractionLevel;
export { type Protocol };

/** Какие протоколы допустимы на уровне взаимодействия. */
export const ALLOWED: Record<Level, readonly Protocol[]> = {
  low: ['tcp', 'http'],
  medium: ['ssh', 'http'],
  high: ['ssh'],
};

/** Учётки-приманки имеют смысл там, где есть вход по SSH (medium, high). */
export const hasCredentials = (level: Level) => level !== 'low';
/** Блок настроек VM показывается только для High. */
export const hasHigh = (level: Level) => level === 'high';

const HOSTNAME = /^(?=.{1,63}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i;
/** Логин учётки — по маске сервера (CredentialInput.username). */
const USERNAME = /^[a-zA-Z0-9_.-]+$/;

const service = z.object({
  protocol: z.enum(PROTOCOLS),
  port: z.coerce.number({ message: 'Порт — число' }).int('Порт — целое число').min(1, 'Порт от 1').max(65535, 'Порт до 65535'),
  banner: z.string().max(200, 'Баннер до 200 символов'),
});

const credential = z.object({
  username: z.string().trim().min(1, 'Введите логин').max(64, 'До 64 символов')
    .regex(USERNAME, 'Логин: латиница, цифры, точка, дефис и подчёркивание'),
  password: z.string().min(1, 'Введите пароль').max(128, 'До 128 символов'),
});

export const ProfileSchema = z.object({
  name: z.string().trim().min(1, 'Введите название').max(80, 'До 80 символов'),
  description: z.string().trim().max(500, 'До 500 символов'),
  level: z.enum(LEVELS),
  hostname: z.string().trim().regex(HOSTNAME, 'Имя хоста: латиница, цифры и дефис, до 63 символов'),
  services: z.array(service).min(1, 'Нужен хотя бы один сервис').max(16, 'Не больше 16 сервисов'),
  credentials: z.array(credential).max(8, 'Не больше 8 учёток'),
  logging: z.object({
    // Контракт: max_payload_bytes 256–8192. В форме удобнее КиБ (1–8).
    payload_limit_kb: z.coerce.number().int().min(1, 'От 1 КиБ').max(8, 'До 8 КиБ'),
  }),
  channel: z.object({
    flush_interval_ms: z.coerce.number().int().min(100, 'От 100 мс').max(5000, 'До 5000 мс'),
    batch_size: z.coerce.number().int().min(1, 'От 1').max(100, 'До 100'),
  }),
  high: z.object({
    ttl_seconds: z.coerce.number().int().min(60, 'От 60 с').max(3600, 'До 3600 с'),
    cpu: z.coerce.number().int().min(1, 'От 1').max(2, 'До 2'),
    memory_mb: z.coerce.number().int().min(1024, 'От 1024 МБ').max(4096, 'До 4096 МБ'),
  }),
}).superRefine((p, ctx) => {
  const allowed = ALLOWED[p.level];
  const seen = new Map<number, number>();
  p.services.forEach((s, i) => {
    if (!allowed.includes(s.protocol)) {
      ctx.addIssue({ code: 'custom', path: ['services', i, 'protocol'], message: `На уровне ${p.level} нельзя ${s.protocol}` });
    }
    const prev = seen.get(s.port);
    if (prev !== undefined) {
      ctx.addIssue({ code: 'custom', path: ['services', i, 'port'], message: `Порт уже занят сервисом №${prev + 1}` });
    } else {
      seen.set(s.port, i);
    }
  });
  if (p.level === 'medium' && !p.services.some((s) => s.protocol === 'ssh')) {
    ctx.addIssue({ code: 'custom', path: ['services'], message: 'Medium нужен хотя бы один SSH-сервис' });
  }
  if (p.level === 'high' && p.services.length !== 1) {
    ctx.addIssue({ code: 'custom', path: ['services'], message: 'High — ровно один SSH-сервис (шлюз к VM)' });
  }
  if (p.level === 'low' && p.credentials.length) {
    ctx.addIssue({ code: 'custom', path: ['credentials'], message: 'У Low нет входа — учётки не нужны' });
  }
});

export type ProfileInput = z.input<typeof ProfileSchema>;
export type ProfileForm = z.output<typeof ProfileSchema>;

/**
 * Собрать тело запроса (ProfileCreate) из значений формы: пересчитать КиБ в
 * байты, добавить блок High только для уровня high, учётки — только где есть
 * вход. Поля, которых нет на уровне, в config не попадают.
 */
export function formToCreate(f: ProfileForm): { name: string; description: string; config: ProfileConfigInput } {
  const config: ProfileConfigInput = {
    interaction_level: f.level,
    hostname: f.hostname,
    services: f.services.map((s) => ({ protocol: s.protocol, port: s.port, banner: s.banner })),
    logging: { max_payload_bytes: f.logging.payload_limit_kb * 1024 },
    channel: { flush_interval_ms: f.channel.flush_interval_ms, batch_size: f.channel.batch_size },
  };
  if (hasHigh(f.level)) config.high = { template: 'ubuntu-ssh-v1', ...f.high };
  if (hasCredentials(f.level)) config.credentials = f.credentials;
  return { name: f.name, description: f.description, config };
}

/**
 * Сервисы после смены уровня. Подходящие уровню — оставляем как есть,
 * иначе подставляем набор по умолчанию, чтобы пользователь не остался с
 * недопустимым протоколом и ошибкой.
 */
export function servicesForLevel<S extends { protocol: string }>(level: Level, services: S[]): S[] | ProfileForm['services'] {
  const allowed: readonly string[] = ALLOWED[level];
  const fits = services.length > 0
    && services.every((s) => allowed.includes(s.protocol))
    && (level !== 'medium' || services.some((s) => s.protocol === 'ssh'))
    && (level !== 'high' || services.length === 1);
  return fits ? services : emptyProfile(level).services;
}

/** Новый профиль по уровню — сразу валидный набор полей формы. */
export function emptyProfile(level: Level = 'low'): ProfileForm {
  const services: ProfileForm['services'] = level === 'low'
    ? [{ protocol: 'http', port: 8080, banner: 'nginx' }]
    : level === 'medium'
      ? [{ protocol: 'ssh', port: 2222, banner: 'SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.10' }]
      : [{ protocol: 'ssh', port: 2224, banner: '' }];
  return {
    name: '',
    description: '',
    level,
    hostname: 'srv-01',
    services,
    credentials: [],
    logging: { payload_limit_kb: 8 },
    channel: { flush_interval_ms: 1500, batch_size: 100 },
    high: { ttl_seconds: 900, cpu: 2, memory_mb: 2048 },
  };
}

/**
 * Значения формы из прочитанного профиля (ProfileConfigPublic). Пароли
 * учёток сервер не отдаёт — подставляются только логины, поле пароля пустое
 * и требует повторного ввода при сохранении.
 */
export function formFromConfig(name: string, description: string, config: {
  interaction_level: Level;
  hostname?: string;
  services: { protocol: Protocol; port: number; banner: string }[];
  logging?: { max_payload_bytes: number };
  channel?: { flush_interval_ms: number; batch_size: number };
  high?: { ttl_seconds: number; cpu: number; memory_mb: number } | null;
  credential_usernames?: string[];
}): ProfileForm {
  const base = emptyProfile(config.interaction_level);
  return {
    name,
    description,
    level: config.interaction_level,
    hostname: config.hostname ?? base.hostname,
    services: config.services.length ? config.services.map((s) => ({ ...s })) : base.services,
    credentials: (config.credential_usernames ?? []).map((username) => ({ username, password: '' })),
    logging: { payload_limit_kb: config.logging ? Math.max(1, Math.round(config.logging.max_payload_bytes / 1024)) : base.logging.payload_limit_kb },
    channel: config.channel ? { ...config.channel } : base.channel,
    high: config.high ? { ttl_seconds: config.high.ttl_seconds, cpu: config.high.cpu, memory_mb: config.high.memory_mb } : base.high,
  };
}
