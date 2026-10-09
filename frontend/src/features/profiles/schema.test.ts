// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { emptyProfile, ProfileSchema, servicesForLevel, type ProfileConfig } from './schema';

const valid = (patch: Partial<ProfileConfig> = {}): ProfileConfig => ({ ...emptyProfile('medium'), name: 'Тестовый сервер', ...patch });
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
      decoy_credentials: [{ username: 'root', password: 'toor' }] };
    expect(issues(p)).toEqual(expect.arrayContaining([
      'services.0.protocol: На уровне low нельзя ssh',
      'decoy_credentials: У Low нет входа — учётки не нужны',
    ]));
  });

  it('Medium без SSH не проходит', () => {
    expect(issues(valid({ services: [{ protocol: 'http', port: 80, banner: '' }] })))
      .toContain('services: Medium нужен хотя бы один SSH-сервис');
  });

  it('High — ровно один SSH-сервис', () => {
    const p = { ...emptyProfile('high'), name: 'x', services: [{ protocol: 'ssh', port: 22, banner: '' }, { protocol: 'ssh', port: 23, banner: '' }] };
    expect(issues(p)).toContain('services: High — ровно один SSH-сервис (шлюз к VM)');
  });

  it('лимиты логирования и канала', () => {
    const p = valid({ logging: { payload_limit_kb: 9, record_tty: true }, channel: { beacon_interval_s: 1, jitter_pct: 80 } });
    expect(issues(p)).toEqual(expect.arrayContaining([
      'logging.payload_limit_kb: До 8 КиБ', 'channel.beacon_interval_s: От 5 с', 'channel.jitter_pct: До 50 %',
    ]));
  });

  it('смена уровня: подходящие сервисы остаются, неподходящие заменяются', () => {
    const http = [{ protocol: 'http' as const, port: 80, banner: 'x' }];
    expect(servicesForLevel('low', http)).toBe(http);
    expect(servicesForLevel('medium', http)).toEqual(emptyProfile('medium').services);
    const twoSsh = [{ protocol: 'ssh' as const, port: 22, banner: '' }, { protocol: 'ssh' as const, port: 23, banner: '' }];
    expect(servicesForLevel('medium', twoSsh)).toBe(twoSsh);
    expect(servicesForLevel('high', twoSsh)).toEqual(emptyProfile('high').services);
    expect(servicesForLevel('low', [])).toEqual(emptyProfile('low').services);
    // Результат для каждого уровня проходит схему.
    for (const level of ['low', 'medium', 'high'] as const) {
      expect(issues({ ...emptyProfile(level), name: 'x', services: servicesForLevel(level, twoSsh) })).toEqual([]);
    }
  });

  it('имя хоста легенды по RFC 1123', () => {
    expect(issues(valid({ legend: { hostname: 'web_01', os: '' } }))[0]).toMatch(/^legend.hostname/);
    expect(issues(valid({ legend: { hostname: '-bad', os: '' } }))[0]).toMatch(/^legend.hostname/);
    expect(issues(valid({ legend: { hostname: 'db-prod-02', os: '' } }))).toEqual([]);
  });
});
