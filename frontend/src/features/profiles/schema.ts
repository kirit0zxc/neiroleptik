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
  medium: ['ssh'],
  high: ['ssh'],
};

/** Учётки-приманки имеют смысл там, где есть вход по SSH (medium, high). */
export const hasCredentials = (level: Level) => level !== 'low';
/** Блок настроек VM показывается только для High. */
export const hasHigh = (level: Level) => level === 'high';

const HOSTNAME = /^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$/;
/** Логин учётки — по маске сервера (CredentialInput.username). */
const USERNAME = /^[a-zA-Z0-9_.-]+$/;
/** Баннер TCP — только печатные ASCII (0x20–0x7E): так требует драйвер Low. */
const PRINTABLE_ASCII = /^[ -~]+$/;

/** На Low драйвер поднимает не больше одного сервиса каждого протокола. */
const LOW_SINGLE: readonly Protocol[] = ['tcp', 'http'];

const service = z.object({
  protocol: z.enum(PROTOCOLS),
  port: z.coerce.number({ message: 'Порт — число' }).int('Порт — целое число').min(1, 'Порт от 1').max(65535, 'Порт до 65535'),
  banner: z.string().max(200, 'Баннер до 200 символов').refine((v) => !/[\r\n\0]/.test(v), 'Баннер должен быть одной строкой'),
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
  services: z.array(service).min(1, 'Нужен хотя бы один сервис').max(8, 'Не больше 8 сервисов'),
  credentials: z.array(credential).max(8, 'Не больше 8 учёток'),
  credentials_mode: z.enum(['preserve', 'replace']),
  preserved_usernames: z.array(z.string()),
  logging: z.object({
    // Байты сохраняются без округления при редактировании существующей конфигурации.
    max_payload_bytes: z.coerce.number().int().min(256, 'От 256 байт').max(8192, 'До 8192 байт'),
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
    if (s.protocol === 'tcp') {
      if (!s.banner) ctx.addIssue({ code: 'custom', path: ['services', i, 'banner'], message: 'У TCP баннер обязателен' });
      else if (!PRINTABLE_ASCII.test(s.banner)) {
        ctx.addIssue({ code: 'custom', path: ['services', i, 'banner'], message: 'Баннер TCP: только латиница, цифры, пробел и знаки ASCII' });
      }
    }
    if (p.level === 'low' && LOW_SINGLE.includes(s.protocol) && p.services.findIndex((x) => x.protocol === s.protocol) < i) {
      ctx.addIssue({ code: 'custom', path: ['services', i, 'protocol'], message: `На Low не больше одного ${s.protocol.toUpperCase()}-сервиса` });
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
  if (hasCredentials(p.level) && (p.credentials_mode === 'preserve' ? !p.preserved_usernames.length : !p.credentials.length)) {
    ctx.addIssue({ code: 'custom', path: ['credentials'], message: 'Medium и High требуют хотя бы одну учётку-приманку' });
  }
  const usernames = new Set<string>();
  p.credentials.forEach((c, i) => {
    if (usernames.has(c.username)) ctx.addIssue({ code: 'custom', path: ['credentials', i, 'username'], message: 'Логины учёток должны быть уникальными' });
    usernames.add(c.username);
  });
  if (p.level === 'low' && p.credentials.length) {
    ctx.addIssue({ code: 'custom', path: ['credentials'], message: 'У Low нет входа — учётки не нужны' });
  }
});

export type ProfileInput = z.input<typeof ProfileSchema>;
export type ProfileForm = z.output<typeof ProfileSchema>;

/**
 * Собрать тело записи профиля. null сохраняет зашифрованные пароли сервера,
 * список явно заменяет их. При переходе на Low отправляется явная очистка.
 */
export function formToCreate(f: ProfileForm): { name: string; description: string; config: ProfileConfigInput } {
  const config: ProfileConfigInput = {
    interaction_level: f.level,
    hostname: f.hostname,
    services: f.services.map((s) => ({ protocol: s.protocol, port: s.port, banner: s.banner })),
    logging: { max_payload_bytes: f.logging.max_payload_bytes },
    credentials: hasCredentials(f.level) && f.credentials_mode === 'preserve' ? null : f.credentials,
    channel: { flush_interval_ms: f.channel.flush_interval_ms, batch_size: f.channel.batch_size },
  };
  if (hasHigh(f.level)) config.high = { template: 'ubuntu-ssh-v1', ...f.high };
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
    && (level !== 'high' || services.length === 1)
    && (level !== 'low' || LOW_SINGLE.every((p) => services.filter((s) => s.protocol === p).length <= 1));
  return fits ? services : emptyProfile(level).services;
}

/** Подпись поля banner: у HTTP это тело ответа заглушки, у TCP и SSH — баннер. */
export function bannerLabel(protocol: string): string {
  return protocol === 'http' ? 'Текст страницы-заглушки' : 'Баннер';
}

/**
 * Протокол для кнопки «+ Сервис» или null, если добавлять нечего: на High
 * сервис ровно один, на Low — не больше одного TCP и одного HTTP.
 */
export function nextServiceProtocol(level: Level, services: readonly { protocol: string }[]): Protocol | null {
  if (level === 'high' || services.length >= 8) return null;
  if (level === 'low') return LOW_SINGLE.find((p) => !services.some((s) => s.protocol === p)) ?? null;
  return ALLOWED[level][0] ?? null;
}

/** Новый профиль: SSH-уровни требуют явного добавления приманочных учёток. */
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
    credentials_mode: 'replace',
    preserved_usernames: [],
    logging: { max_payload_bytes: 8192 },
    channel: { flush_interval_ms: 1500, batch_size: 100 },
    high: { ttl_seconds: 900, cpu: 2, memory_mb: 2048 },
  };
}

/**
 * Значения формы из прочитанного профиля (ProfileConfigPublic). Пароли
 * учёток сервер не отдаёт: по умолчанию сохраняем их, не запрашивая секреты.
 * Логины нужны для показа имеющихся учёток и явного перехода к замене.
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
    credentials: [],
    credentials_mode: config.credential_usernames?.length && hasCredentials(config.interaction_level) ? 'preserve' : 'replace',
    preserved_usernames: config.credential_usernames ?? [],
    logging: { max_payload_bytes: config.logging?.max_payload_bytes ?? base.logging.max_payload_bytes },
    channel: config.channel ? { ...config.channel } : base.channel,
    high: config.high ? { ttl_seconds: config.high.ttl_seconds, cpu: config.high.cpu, memory_mb: config.high.memory_mb } : base.high,
  };
}
