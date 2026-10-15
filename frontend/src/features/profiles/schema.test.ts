// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { bannerLabel, emptyProfile, formFromConfig, formToCreate, nextServiceProtocol, ProfileSchema, servicesForLevel, type ProfileForm } from './schema';

const valid = (patch: Partial<ProfileForm> = {}): ProfileForm => ({ ...emptyProfile('medium'), name: 'Тестовый сервер', credentials: [{ username: 'admin', password: 'demo' }], ...patch });
const issues = (p: unknown) => {
  const r = ProfileSchema.safeParse(p);
  return r.success ? [] : r.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`);
};

describe('схема профиля (F8)', () => {
  it('новый SSH-профиль требует учётку даже при заданном имени', () => {
    for (const level of ['low', 'medium', 'high'] as const) {
      expect(issues({ ...emptyProfile(level), name: 'x' })).toEqual(level === 'low' ? [] : ['credentials: Medium и High требуют хотя бы одну учётку-приманку']);
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
    const p = valid({ services: [{ protocol: 'ssh', port: 22, banner: '' }, { protocol: 'ssh', port: 22, banner: '' }] });
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

  it('Low: не больше одного TCP и одного HTTP (драйвер участника 3)', () => {
    const low = (services: ProfileForm['services']) => issues({ ...emptyProfile('low'), name: 'x', services });
    expect(low([{ protocol: 'tcp', port: 23, banner: 'BusyBox telnetd' }, { protocol: 'http', port: 80, banner: '' }])).toEqual([]);
    expect(low([{ protocol: 'http', port: 80, banner: '' }, { protocol: 'http', port: 81, banner: '' }]))
      .toEqual(['services.1.protocol: На Low не больше одного HTTP-сервиса — удалите лишний или выберите другой протокол']);
    expect(low([{ protocol: 'tcp', port: 23, banner: 'a' }, { protocol: 'tcp', port: 24, banner: 'b' }]))
      .toEqual(['services.1.protocol: На Low не больше одного TCP-сервиса — удалите лишний или выберите другой протокол']);
  });

  it('баннер TCP обязателен и только из печатных ASCII', () => {
    const tcp = (banner: string) => issues({ ...emptyProfile('low'), name: 'x', services: [{ protocol: 'tcp', port: 23, banner }] });
    expect(tcp('')).toEqual(['services.0.banner: У TCP баннер обязателен']);
    expect(tcp('Привет')).toEqual(['services.0.banner: Баннер TCP: только латиница, цифры, пробел и знаки ASCII']);
    for (const bad of ['a\tb', 'a\u007Fb']) {
      expect(tcp(bad)).toEqual(['services.0.banner: Баннер TCP: только латиница, цифры, пробел и знаки ASCII']);
    }
    expect(tcp('   ')).toEqual(['services.0.banner: У TCP баннер обязателен']);
    expect(tcp('a\nb')).toContain('services.0.banner: Баннер должен быть одной строкой');
    expect(tcp(' ~SSH-2.0 x!')).toEqual([]);
    // У HTTP баннер (текст заглушки) может быть пустым и не-ASCII.
    expect(issues({ ...emptyProfile('low'), name: 'x', services: [{ protocol: 'http', port: 80, banner: 'Привет' }] })).toEqual([]);
  });

  it('Medium: свой SSH-баннер агент пока не принимает — поле обязано быть пустым', () => {
    // Непустой баннер SSH на Medium — ошибка (агент ответил бы UNSUPPORTED_PROFILE).
    expect(issues(valid({ services: [{ protocol: 'ssh', port: 2222, banner: 'SSH-2.0-OpenSSH_8.9p1' }] })))
      .toEqual(['services.0.banner: На Medium баннер SSH пока не поддерживается — оставьте пустым']);
    expect(issues(valid({ services: [{ protocol: 'ssh', port: 2222, banner: '   ' }] }))).toEqual([]);
    // Шаблон нового Medium-профиля даёт пустой баннер, чтобы ловушка запустилась сразу.
    expect(emptyProfile('medium').services[0]!.banner).toBe('');
    // На High своё правило баннера нет — непустой допустим.
    expect(issues({ ...emptyProfile('high'), name: 'x', credentials: [{ username: 'root', password: 'demo' }],
      services: [{ protocol: 'ssh', port: 2224, banner: 'SSH-2.0-OpenSSH_8.9p1' }] })).toEqual([]);
  });

  it('смена уровня на Low: два HTTP не подходят — набор по умолчанию', () => {
    const two = [{ protocol: 'http', port: 80, banner: '' }, { protocol: 'http', port: 81, banner: '' }];
    expect(servicesForLevel('low', two)).toEqual(emptyProfile('low').services);
  });

  it('High: значения по умолчанию как в центре — TTL 900 с, 2 vCPU, 2048 МБ', () => {
    expect(emptyProfile('high').high).toEqual({ ttl_seconds: 900, cpu: 2, memory_mb: 2048 });
  });

  it('подпись поля баннера зависит от протокола', () => {
    expect(bannerLabel('http')).toBe('Текст страницы-заглушки');
    expect(bannerLabel('tcp')).toBe('Баннер');
    expect(bannerLabel('ssh')).toBe('Баннер');
  });

  it('«+ Сервис» на Low добавляет недостающий протокол и пропадает, когда есть оба', () => {
    expect(nextServiceProtocol('low', [{ protocol: 'http' }])).toBe('tcp');
    expect(nextServiceProtocol('low', [{ protocol: 'tcp' }])).toBe('http');
    expect(nextServiceProtocol('low', [{ protocol: 'tcp' }, { protocol: 'http' }])).toBeNull();
    expect(nextServiceProtocol('medium', [{ protocol: 'ssh' }])).toBe('ssh');
    expect(nextServiceProtocol('high', [{ protocol: 'ssh' }])).toBeNull();
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
      logging: { max_payload_bytes: 8193 },
      channel: { flush_interval_ms: 50, batch_size: 200 },
    });
    expect(issues(p)).toEqual(expect.arrayContaining([
      'logging.max_payload_bytes: До 8192 байт', 'channel.flush_interval_ms: От 100 мс', 'channel.batch_size: До 100',
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
      expect(issues({ ...emptyProfile(level), name: 'x', credentials: level === 'low' ? [] : [{ username: 'demo', password: 'x' }], services: servicesForLevel(level, twoSsh) })).toEqual([]);
    }
  });
});

describe('сборка тела запроса по контракту (formToCreate)', () => {
  it('low: точные байты, явная очистка учёток и без High', () => {
    const { name, description, config } = formToCreate({ ...emptyProfile('low'), name: 'L', description: 'd' });
    expect(name).toBe('L');
    expect(description).toBe('d');
    expect(config.interaction_level).toBe('low');
    expect(config.logging?.max_payload_bytes).toBe(8 * 1024);
    expect(config.credentials).toEqual([]);
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

describe('редактирование прочитанной конфигурации', () => {
  const config = { interaction_level: 'medium' as const, hostname: 'ssh-node',
    services: [{ protocol: 'ssh' as const, port: 22, banner: '' }],
    credential_usernames: ['admin'], logging: { max_payload_bytes: 256 } };

  it('сохраняет существующие пароли через null без повторного ввода', () => {
    const form = formFromConfig('Название', '', config);
    expect(issues(form)).toEqual([]);
    expect(formToCreate(form).config.credentials).toBeNull();
    expect(form.credentials).toEqual([]);
  });

  it.each([256, 257, 1536, 8191, 8192])('сохраняет %i байт без округления', (bytes) => {
    const form = formFromConfig('Название', '', { ...config, logging: { max_payload_bytes: bytes } });
    expect(issues(form)).toEqual([]);
    expect(formToCreate(form).config.logging?.max_payload_bytes).toBe(bytes);
  });

  it('явная замена требует хотя бы одну валидную учётку', () => {
    const form = formFromConfig('Название', '', config);
    form.credentials_mode = 'replace';
    expect(issues(form)).toContain('credentials: Medium и High требуют хотя бы одну учётку-приманку');
    form.credentials = [{ username: 'admin', password: '' }];
    expect(issues(form)).toContain('credentials.0.password: Введите пароль');
    form.credentials[0]!.password = 'new-secret';
    expect(issues(form)).toEqual([]);
    expect(formToCreate(form).config.credentials).toEqual([{ username: 'admin', password: 'new-secret' }]);
  });

  it('нет сохранённых логинов — нельзя сохранить пустые Medium/High', () => {
    for (const level of ['medium', 'high'] as const) {
      const form = { ...emptyProfile(level), name: 'x', credentials_mode: 'preserve' as const };
      expect(issues(form)).toContain('credentials: Medium и High требуют хотя бы одну учётку-приманку');
    }
  });

  it('Medium запрещает HTTP даже рядом с SSH', () => {
    expect(issues(valid({ services: [...config.services, { protocol: 'http', port: 80, banner: '' }] })))
      .toContain('services.1.protocol: На уровне medium нельзя http');
  });

  it('повтор логина и многострочный баннер отклоняются до запроса', () => {
    expect(issues(valid({ credentials: [{ username: 'same', password: 'x' }, { username: 'same', password: 'y' }] })))
      .toContain('credentials.1.username: Логины учёток должны быть уникальными');
    expect(issues(valid({ services: [{ protocol: 'ssh', port: 22, banner: 'one\ntwo' }] })))
      .toContain('services.0.banner: Баннер должен быть одной строкой');
  });
});
