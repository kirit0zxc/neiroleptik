import { describe, expect, it } from 'vitest';
import { ApiError } from '../../shared/api/client';
import { artifactErrorMessage, filenameFrom, formatBytes, IocPeriod } from './logic';

const at = (iso: string) => new Date(iso);

describe('период экспорта IoC (F18)', () => {
  it('по умолчанию — последние 24 часа, без секунд', () => {
    const p = IocPeriod.lastDay(at('2026-10-11T12:34:56.789Z'));
    expect(p.to.toISOString()).toBe('2026-10-11T12:34:00.000Z');
    expect(p.from.toISOString()).toBe('2026-10-10T12:34:00.000Z');
    expect(p.validate()).toBeNull();
  });

  it('обе даты всегда в запросе, ловушка — необязательно', () => {
    const p = new IocPeriod(at('2026-10-10T00:00:00Z'), at('2026-10-11T00:00:00Z'));
    expect(p.toQuery().toString()).toBe('from=2026-10-10T00%3A00%3A00.000Z&to=2026-10-11T00%3A00%3A00.000Z');
    expect(p.toQuery('d1').get('deployment_id')).toBe('d1');
  });

  it.each([
    ['пустая дата', IocPeriod.fromInputs('', '2026-10-11T00:00'), 'Укажите обе даты.'],
    ['начало не раньше конца', new IocPeriod(at('2026-10-11T00:00:00Z'), at('2026-10-11T00:00:00Z')), 'Начало периода должно быть раньше конца.'],
    ['больше 31 дня', new IocPeriod(at('2026-09-01T00:00:00Z'), at('2026-10-02T00:00:01Z')), 'Период — не больше 31 дня.'],
  ])('%s — ошибка до запроса', (_name, p, text) => {
    expect(p.validate()).toBe(text);
  });

  it('ровно 31 день допустим', () => {
    expect(new IocPeriod(at('2026-09-01T00:00:00Z'), at('2026-10-02T00:00:00Z')).validate()).toBeNull();
  });
});

describe('имя скачиваемого файла', () => {
  it('filename* важнее filename, путь и управляющие символы отрезаются', () => {
    expect(filenameFrom("attachment; filename=\"x.sh\"; filename*=UTF-8''%D1%84%D0%B0%D0%B9%D0%BB.sh", 'f')).toBe('файл.sh');
    expect(filenameFrom('attachment; filename="../../etc/passwd"', 'f')).toBe('passwd');
    expect(filenameFrom('attachment; filename="a\u0007b.bin"', 'f')).toBe('ab.bin');
    expect(filenameFrom(null, 'fallback.bin')).toBe('fallback.bin');
    expect(filenameFrom('attachment; filename=""', 'fallback.bin')).toBe('fallback.bin');
  });
});

describe('ошибки скачивания артефакта', () => {
  it.each([
    [new ApiError(409, 'x', [], 'ARTIFACT_PENDING'), 'Файл ещё не готов — проверьте позже.'],
    [new ApiError(409, 'x', [], 'ARTIFACT_FAILED'), 'Файл не удалось получить.'],
    [new ApiError(410, 'x', [], 'ARTIFACT_EXPIRED'), 'Файл удалён по сроку хранения.'],
    [new ApiError(403, 'x'), 'Скачивать артефакты могут только оператор и администратор.'],
    [new ApiError(503, 'Центр недоступен'), 'Центр недоступен'],
  ])('%s', (err, text) => {
    expect(artifactErrorMessage(err)).toBe(text);
  });
});

describe('размер файла', () => {
  it('Б, КБ, МБ и неизвестный', () => {
    expect([formatBytes(null), formatBytes(31), formatBytes(1536), formatBytes(5 * 1024 * 1024)]).toEqual(['—', '31 Б', '1,5 КБ', '5,0 МБ']);
  });
});
