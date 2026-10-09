import { z } from 'zod';

/**
 * Профиль ловушки (specs/frontend.md, F8 и «Поля профиля»).
 * Черновик до утверждения OpenAPI: имена полей — по плану команды.
 */

export const LEVELS = ['low', 'medium', 'high'] as const;
export const PROTOCOLS = ['tcp', 'http', 'ssh'] as const;
export type Level = (typeof LEVELS)[number];
export type Protocol = (typeof PROTOCOLS)[number];

/** Какие протоколы допустимы на уровне (план команды, «Уровни ловушек»). */
export const ALLOWED: Record<Level, readonly Protocol[]> = {
  low: ['tcp', 'http'],
  medium: ['ssh', 'http'],
  high: ['ssh'],
};

/** Учётки-приманки имеют смысл там, где есть вход по SSH. */
export const hasCredentials = (level: Level) => level !== 'low';

const HOSTNAME = /^(?=.{1,63}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i;

const service = z.object({
  protocol: z.enum(PROTOCOLS),
  port: z.coerce.number({ message: 'Порт — число' }).int('Порт — целое число').min(1, 'Порт от 1').max(65535, 'Порт до 65535'),
  banner: z.string().max(256, 'Баннер до 256 символов'),
});

const credential = z.object({
  username: z.string().trim().min(1, 'Введите логин').max(64, 'До 64 символов'),
  password: z.string().min(1, 'Введите пароль').max(128, 'До 128 символов'),
});

export const ProfileSchema = z.object({
  name: z.string().trim().min(1, 'Введите название').max(64, 'До 64 символов'),
  level: z.enum(LEVELS),
  services: z.array(service).min(1, 'Нужен хотя бы один сервис').max(16, 'Не больше 16 сервисов'),
  decoy_credentials: z.array(credential).max(20, 'Не больше 20 учёток'),
  logging: z.object({
    payload_limit_kb: z.coerce.number().int().min(1, 'От 1 КиБ').max(8, 'До 8 КиБ'),
    record_tty: z.boolean(),
  }),
  channel: z.object({
    beacon_interval_s: z.coerce.number().int().min(5, 'От 5 с').max(300, 'До 300 с'),
    jitter_pct: z.coerce.number().int().min(0, 'От 0 %').max(50, 'До 50 %'),
  }),
  legend: z.object({
    hostname: z.string().trim().regex(HOSTNAME, 'Имя хоста: латиница, цифры и дефис, до 63 символов'),
    os: z.string().trim().max(64, 'До 64 символов'),
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
  if (p.level === 'low' && p.decoy_credentials.length) {
    ctx.addIssue({ code: 'custom', path: ['decoy_credentials'], message: 'У Low нет входа — учётки не нужны' });
  }
});

export type ProfileInput = z.input<typeof ProfileSchema>;
export type ProfileConfig = z.output<typeof ProfileSchema>;

/**
 * Сервисы после смены уровня. Подходящие уровню — оставляем как есть,
 * иначе подставляем набор по умолчанию: иначе пользователь получает
 * ошибку, а в выпадающем списке не видит выбранный протокол.
 */
export function servicesForLevel<S extends { protocol: string }>(level: Level, services: S[]): S[] | ProfileConfig['services'] {
  const allowed: readonly string[] = ALLOWED[level];
  const fits = services.length > 0
    && services.every((s) => allowed.includes(s.protocol))
    && (level !== 'medium' || services.some((s) => s.protocol === 'ssh'))
    && (level !== 'high' || services.length === 1);
  return fits ? services : emptyProfile(level).services;
}

/** Новый профиль по уровню — сразу валидный набор сервисов. */
export function emptyProfile(level: Level = 'low'): ProfileConfig {
  const services: ProfileConfig['services'] = level === 'low'
    ? [{ protocol: 'http', port: 8080, banner: 'nginx' }]
    : level === 'medium'
      ? [{ protocol: 'ssh', port: 2222, banner: 'SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.10' }]
      : [{ protocol: 'ssh', port: 2224, banner: '' }];
  return {
    name: '',
    level,
    services,
    decoy_credentials: [],
    logging: { payload_limit_kb: 8, record_tty: level !== 'low' },
    channel: { beacon_interval_s: 30, jitter_pct: 20 },
    legend: { hostname: 'srv-01', os: 'Ubuntu 22.04' },
  };
}
