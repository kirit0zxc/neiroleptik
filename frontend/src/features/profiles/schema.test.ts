// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { emptyProfile, formToCreate, ProfileSchema, servicesForLevel, type ProfileForm } from './schema';

const valid = (patch: Partial<ProfileForm> = {}): ProfileForm => ({ ...emptyProfile('medium'), name: 'Тестовый сервер', ...patch });
const issues = (p: unknown) => {
  const r = ProfileSchema.safeParse(p);
  return r.success ? [] : r.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`);
};

describe('схема профиля (F8)', () => {
  it('профиль по умолчанию для каждого уровня проходит, если задано имя', () => {
    for (const level of ['low', 'medium', 'high'] as const) {
      expect(issues({ ...emptyProfile(level), name: 'x' })).toEqual([]);
    }
  });

  it('пустое имя и порт вне диапазона', () => {
    const p = valid({ name: '  ', services: [{ protocol: 'ssh', port: 70000, banner: '' }] });
    expect(issues(p)).toEqual(expect.arrayContaining(['name: Введите название', 'services.0.port: Порт до 65535']));
  });

  it('порт из поля ввода (строка) приводится к числу', () => {
    const r = ProfileSchema.safeParse({ ...valid(), services: [{ protocol: 'ssh', port: '2222', banner: '' }] });
    expect(r.success && r.data.services[0]!.port).toBe(2222);
  });

  it('повтор порта отмечается у второго сервиса', () => {
    const p = valid({ services: [{ protocol: 'ssh', port: 22, banner: '' }, { protocol: 'http', port: 22, banner: '' }] });
    expect(issues(p)).toEqual(['services.1.port: Порт уже занят сервисом №1']);
  });

  it('Low: только tcp/http и без учёток', () => {
    const p = { ...emptyProfile('low'), name: 'x', services: [{ protocol: 'ssh', port: 22, banner: '' }],
      credentials: [{ username: 'root', password: 'toor' }] };
    expect(issues(p)).toEqual(expect.arrayContaining([
      'services.0.protocol: На уровне low нельзя ssh',
      'credentials: У Low нет входа — учётки не нужны',
    ]));
  });

  it('учётка: логин по маске сервера', () => {
    expect(issues(valid({ credentials: [{ username: 'a b', password: 'x' }] })))
      .toContain('credentials.0.username: Логин: латиница, цифры, точка, дефис и подчёркивание');
    expect(issues(valid({ credentials: [{ username: 'ok-user.1', password: 'x' }] }))).toEqual([]);
  });

  it('Medium без SSH не проходит', () => {
    expect(issues(valid({ services: [{ protocol: 'http', port: 80, banner: '' }] })))
      .toContain('services: Medium нужен хотя бы один SSH-сервис');
  });

  it('High — ровно один SSH-сервис', () => {
    const p = { ...emptyProfile('high'), name: 'x', services: [{ protocol: 'ssh', port: 22, banner: '' }, { protocol: 'ssh', port: 23, banner: '' }] };
    expect(issues(p)).toContain('services: High — ровно один SSH-сервис (шлюз к VM)');
  });

  it('лимиты логирования, канала и High', () => {
    const p = valid({
      logging: { payload_limit_kb: 9 },
      channel: { flush_interval_ms: 50, batch_size: 200 },
    });
    expect(issues(p)).toEqual(expect.arrayContaining([
      'logging.payload_limit_kb: До 8 КиБ', 'channel.flush_interval_ms: От 100 мс', 'channel.batch_size: До 100',
    ]));
    const h = { ...emptyProfile('high'), name: 'x', high: { ttl_seconds: 10, cpu: 4, memory_mb: 512 } };
    expect(issues(h)).toEqual(expect.arrayContaining([
      'high.ttl_seconds: От 60 с', 'high.cpu: До 2', 'high.memory_mb: От 1024 МБ',
    ]));
  });

  it('имя хоста легенды по RFC 1123', () => {
    expect(issues(valid({ hostname: 'web_01' }))[0]).toMatch(/^hostname/);
    expect(issues(valid({ hostname: '-bad' }))[0]).toMatch(/^hostname/);
    expect(issues(valid({ hostname: 'db-prod-02' }))).toEqual([]);
  });

  it('смена уровня: подходящие сервисы остаются, неподходящие заменяются', () => {
    const http = [{ protocol: 'http' as const, port: 80, banner: 'x' }];
    expect(servicesForLevel('low', http)).toBe(http);
    expect(servicesForLevel('medium', http)).toEqual(emptyProfile('medium').services);
    const twoSsh = [{ protocol: 'ssh' as const, port: 22, banner: '' }, { protocol: 'ssh' as const, port: 23, banner: '' }];
    expect(servicesForLevel('medium', twoSsh)).toBe(twoSsh);
    expect(servicesForLevel('high', twoSsh)).toEqual(emptyProfile('high').services);
    expect(servicesForLevel('low', [])).toEqual(emptyProfile('low').services);
    for (const level of ['low', 'medium', 'high'] as const) {
      expect(issues({ ...emptyProfile(level), name: 'x', services: servicesForLevel(level, twoSsh) })).toEqual([]);
    }
  });
});

describe('сборка тела запроса по контракту (formToCreate)', () => {
  it('low: КиБ→байты, без учёток и High', () => {
    const { name, description, config } = formToCreate({ ...emptyProfile('low'), name: 'L', description: 'd' });
    expect(name).toBe('L');
    expect(description).toBe('d');
    expect(config.interaction_level).toBe('low');
    expect(config.logging?.max_payload_bytes).toBe(8 * 1024);
    expect(config.credentials).toBeUndefined();
    expect(config.high).toBeUndefined();
    expect(config.channel).toEqual({ flush_interval_ms: 1500, batch_size: 100 });
  });

  it('high: добавляет блок High с шаблоном и учётки', () => {
    const form: ProfileForm = { ...emptyProfile('high'), name: 'H', credentials: [{ username: 'deploy', password: 'x' }] };
    const { config } = formToCreate(form);
    expect(config.high).toEqual({ template: 'ubuntu-ssh-v1', ttl_seconds: 900, cpu: 2, memory_mb: 2048 });
    expect(config.credentials).toEqual([{ username: 'deploy', password: 'x' }]);
  });
});
