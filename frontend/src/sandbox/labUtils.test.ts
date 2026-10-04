// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { fileLabel, stableJson } from './labUtils';

describe('fileLabel', () => {
  it('имя файла из пути, раскодированное', () => {
    expect(fileLabel('anim/vrma/Happy%20Idle.vrma')).toBe('Happy Idle.vrma');
    expect(fileLabel('peace')).toBe('peace');
  });

  it('одиночный % (путь записан руками) — как есть, без исключения', () => {
    expect(fileLabel('anim/100%.vrma')).toBe('100%.vrma');
  });
});

describe('stableJson', () => {
  it('порядок ключей, в том числе вложенных, не важен', () => {
    expect(stableJson({ b: 1, a: { d: 2, c: 3 } })).toBe(stableJson({ a: { c: 3, d: 2 }, b: 1 }));
  });

  it('разные значения различаются, массивы не сортируются', () => {
    expect(stableJson({ a: 1 })).not.toBe(stableJson({ a: 2 }));
    expect(stableJson([2, 1])).not.toBe(stableJson([1, 2]));
  });

  it('объект без прототипа (как кадры клипов) сериализуется', () => {
    const o = Object.assign(Object.create(null), { z: 1, y: 2 });
    expect(stableJson(o)).toBe('{"y":2,"z":1}');
  });
});
