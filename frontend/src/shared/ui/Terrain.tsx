import { useEffect, useRef } from 'react';
import s from './terrain.module.css';

/** Холмы рельефа: [x, глубина, радиус, высота] в долях полотна; по глубине — периодичны. */
const HILLS = [[-.38, .25, .11, 1.5], [-.15, .55, .09, 1.1], [.08, .3, .12, 1.7], [.3, .6, .1, 1.3], [.45, .2, .08, 1.2],
  [-.45, .75, .1, 1], [.2, .85, .07, .8], [-.05, .9, .06, .7], [0, .12, .07, .9], [-.27, .05, .06, .8], [.36, .4, .05, .7]] as const;
const COLS = 64;
const ROWS = 34;
/**
 * Сетка продолжается ниже края экрана: ближние холмы приподнимают строки,
 * и без запаса под ними оставалась бы полоса без линий.
 */
const BELOW = 1.6;

/**
 * Слои сетки снизу вверх по экрану (specs/frontend.md, 6a): каждый — свой
 * рельеф от горизонта `top` (доля высоты полотна) до самого низа экрана.
 * Дальние выше, бледнее и ниже по амплитуде; ближний рисуется последним и
 * закрывает дальние.
 */
export const LAYERS = [
  { top: 0, alpha: .3, amp: .55, shift: .37 },
  { top: .28, alpha: .55, amp: .8, shift: .71 },
  { top: .5, alpha: 1, amp: 1, shift: 0 },
] as const;

/** Рельеф одного слоя: высоты и раскладка строк. Статичен — без анимации. */
export class TerrainField {
  /** Высота рельефа в точке x ∈ [-.5, .5] на глубине z; `shift` даёт слою свой узор холмов. */
  static height(x: number, z: number, shift = 0): number {
    const zw = z + shift;
    let y = Math.sin(x * 11 + zw * 2 * Math.PI) * .12;
    for (const [hx, hz, r, a] of HILLS) {
      // Расстояние по глубине — по кругу, чтобы сдвиг слоя не обрезал холмы.
      const dz = ((((zw - hz) % 1) + 1.5) % 1) - .5;
      y += a * Math.exp(-((x - hx) ** 2 + (dz * .45) ** 2) / (2 * r * r));
    }
    return y;
  }

  /**
   * Глубины строк от горизонта (0): глубина 1 — нижний край экрана, строки
   * идут и дальше (до BELOW), чтобы низ экрана всегда был покрыт сеткой.
   */
  static rows(): number[] {
    return Array.from({ length: Math.ceil(ROWS * BELOW) + 1 }, (_, j) => j / ROWS);
  }

  /** Экранная точка слоя: горизонт на `top·h`, глубина 1 — нижний край полотна. */
  static point(i: number, z: number, w: number, h: number, layer: { top: number; amp: number; shift: number }): [number, number] {
    const x = i / COLS - .5;
    const k = .35 + z * 1.4;
    const band = h * (1 - layer.top);
    const y = h * layer.top + band * z - TerrainField.height(x, z, layer.shift) * 55 * k * layer.amp * (band / h);
    return [w / 2 + x * w * k, y];
  }
}

/** Отрисовать все слои: от дальнего к ближнему, ближние холмы закрывают дальние линии. */
function draw(c: HTMLCanvasElement) {
  const w = c.clientWidth, h = c.clientHeight;
  const g = w && h ? c.getContext('2d') : null;
  if (!g) return;
  const dpr = window.devicePixelRatio || 1;
  c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const css = getComputedStyle(c);
  g.strokeStyle = css.getPropertyValue('--terrain-line').trim() || '#b9b3ff';
  g.fillStyle = css.getPropertyValue('--terrain-fill').trim() || '#07070e';
  g.lineWidth = 1;
  for (const layer of LAYERS) {
    g.globalAlpha = layer.alpha;
    let prev: [number, number][] | null = null;
    for (const z of TerrainField.rows()) {
      const row = Array.from({ length: COLS + 1 }, (_, i) => TerrainField.point(i, z, w, h, layer));
      const path = () => { g.beginPath(); row.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y))); };
      // Заливка под строкой непрозрачна — прячет дальние линии этого и прежних слоёв.
      g.globalAlpha = 1;
      path(); g.lineTo(w * 3, h + 400); g.lineTo(-w * 2, h + 400); g.closePath(); g.fill();
      g.globalAlpha = layer.alpha;
      path(); g.stroke();
      const back = prev;
      if (back) row.forEach(([x, y], i) => { const p = back[i]; if (!p) return; g.beginPath(); g.moveTo(p[0], p[1]); g.lineTo(x, y); g.stroke(); });
      prev = row;
    }
  }
  g.globalAlpha = 1;
}

/** Декоративный фон: звёзды и слои каркасного рельефа от самого низа экрана. */
export function Terrain() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    // Перерисовка не чаще кадра; при переносе окна на экран с другой плотностью пикселей — тоже.
    let frame = 0;
    const redraw = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => draw(c)); };
    let dpr: MediaQueryList | null = null;
    const watchDpr = () => {
      dpr?.removeEventListener('change', onDpr);
      dpr = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
      dpr.addEventListener('change', onDpr);
    };
    const onDpr = () => { watchDpr(); redraw(); };
    draw(c);
    watchDpr();
    window.addEventListener('resize', redraw);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', redraw);
      dpr?.removeEventListener('change', onDpr);
    };
  }, []);
  return (
    <div className={s.backdrop} aria-hidden="true">
      <canvas ref={ref} className={s.canvas} />
    </div>
  );
}
