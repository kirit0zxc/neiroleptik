// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { backdropUrl, cssUrl } from './backdrop';

describe('backdropUrl', () => {
  it('true — картинка по умолчанию из папки ассетов', () => {
    expect(backdropUrl(true)).toBe('/mascot/bg/room.webp');
    expect(backdropUrl(true, '/static/yumi/')).toBe('/static/yumi/bg/room.webp');
  });

  it('assetsBase без слэша на конце — как у модели и клипов', () => {
    expect(backdropUrl(true, '/static/yumi')).toBe('/static/yumi/bg/room.webp');
  });

  it('строка: абсолютный адрес как есть, относительный — от assetsBase', () => {
    expect(backdropUrl('/img/space.webp')).toBe('/img/space.webp');
    expect(backdropUrl('https://cdn.test/a.webp')).toBe('https://cdn.test/a.webp');
    expect(backdropUrl('bg/grid.webp', '/m/')).toBe('/m/bg/grid.webp');
  });

  it('false, пустая строка и одни пробелы — без фона', () => {
    expect(backdropUrl(false)).toBeNull();
    expect(backdropUrl('')).toBeNull();
    expect(backdropUrl('   ')).toBeNull();
  });
});

describe('cssUrl', () => {
  it('обычный адрес — в кавычках', () => {
    expect(cssUrl('/mascot/bg/room.webp')).toBe('url("/mascot/bg/room.webp")');
  });

  it('кавычки, обратный слэш и перевод строки экранируются', () => {
    expect(cssUrl('/a"b\\c\nd.webp')).toBe('url("/a\\"b\\\\c\\a d.webp")');
  });

  it('адрес с кавычкой даёт действительное значение свойства', () => {
    const el = document.createElement('div');
    const tricky = '/a"b.webp';
    el.style.backgroundImage = cssUrl(tricky);
    expect(el.style.backgroundImage).not.toBe('');
  });
});
