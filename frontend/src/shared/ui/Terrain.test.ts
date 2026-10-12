import { describe, expect, it } from 'vitest';
import { LAYERS, TerrainField } from './Terrain';

describe('рельеф фона: слои сетки от низа экрана', () => {
  it('строки от горизонта и ниже нижнего края — низ экрана всегда под сеткой', () => {
    const rows = TerrainField.rows();
    expect(rows[0]).toBe(0);
    expect(rows).toContain(1);
    expect(rows.at(-1)!).toBeGreaterThan(1.5);
    expect(rows.every((z, i) => i === 0 || z > rows[i - 1]!)).toBe(true);
  });

  it('каждый слой начинается со своего горизонта и доходит до низа полотна', () => {
    const w = 1000, h = 400;
    for (const layer of LAYERS) {
      // Центр на горизонте и у зрителя; на краю x = −.5 рельеф почти плоский — сравниваем с допуском.
      const [, yTop] = TerrainField.point(0, 0, w, h, { ...layer, amp: 0 });
      const [, yBottom] = TerrainField.point(0, 1, w, h, { ...layer, amp: 0 });
      expect(yTop).toBeCloseTo(h * layer.top);
      expect(yBottom).toBeCloseTo(h);
    }
  });

  it('слои — снизу вверх по экрану, дальние бледнее', () => {
    for (let i = 1; i < LAYERS.length; i++) {
      expect(LAYERS[i]!.top).toBeGreaterThan(LAYERS[i - 1]!.top);
      expect(LAYERS[i]!.alpha).toBeGreaterThan(LAYERS[i - 1]!.alpha);
    }
  });

  it('у слоёв разный узор холмов', () => {
    expect(TerrainField.height(.1, .3, LAYERS[0].shift)).not.toBeCloseTo(TerrainField.height(.1, .3, LAYERS[2].shift), 3);
  });
});
