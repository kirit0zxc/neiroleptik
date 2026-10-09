import { useEffect, useRef } from 'react';
import s from './terrain.module.css';

/** Холмы рельефа: [x, глубина, радиус, высота] в долях полотна. */
const HILLS = [[-.38, .25, .11, 1.5], [-.15, .55, .09, 1.1], [.08, .3, .12, 1.7], [.3, .6, .1, 1.3], [.45, .2, .08, 1.2],
  [-.45, .75, .1, 1], [.2, .85, .07, .8], [-.05, .9, .06, .7], [0, .12, .07, .9], [-.27, .05, .06, .8], [.36, .4, .05, .7]] as const;
const COLS = 64;
const ROWS = 34;

/** Отрисовать каркасный рельеф: от горизонта к зрителю, ближние холмы закрывают дальние линии. */
function draw(c: HTMLCanvasElement) {
  const w = c.clientWidth, h = c.clientHeight;
  const g = w && h ? c.getContext('2d') : null;
  if (!g) return;
  const dpr = window.devicePixelRatio || 1;
  c.width = w * dpr; c.height = h * dpr;
  g.scale(dpr, dpr);
  const css = getComputedStyle(c);
  g.strokeStyle = css.getPropertyValue('--terrain-line').trim() || '#b9b3ff';
  g.fillStyle = css.getPropertyValue('--terrain-fill').trim() || '#07070e';
  g.lineWidth = 1;
  const at = (i: number, j: number): [number, number] => {
    const x = i / COLS - .5, z = j / ROWS;
    let y = Math.sin(x * 11 + z * 6) * .12;
    for (const [hx, hz, r, a] of HILLS) y += a * Math.exp(-((x - hx) ** 2 + ((z - hz) * .45) ** 2) / (2 * r * r));
    const k = .35 + z * 1.4;
    return [w / 2 + x * w * k, h * (.18 + z * .82) - y * 55 * k];
  };
  let prev: [number, number][] | null = null;
  for (let j = 0; j <= ROWS; j++) {
    const row = Array.from({ length: COLS + 1 }, (_, i) => at(i, j));
    const line = () => { g.beginPath(); row.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y))); };
    // Заливка под строкой прячет всё, что дальше и ниже, — так холмы закрывают дальние линии.
    line(); g.lineTo(w * 3, h + 400); g.lineTo(-w * 2, h + 400); g.closePath(); g.fill();
    line(); g.stroke();
    const back = prev;
    if (back) row.forEach(([x, y], i) => { const p = back[i]; if (!p) return; g.beginPath(); g.moveTo(p[0], p[1]); g.lineTo(x, y); g.stroke(); });
    prev = row;
  }
}

/** Декоративный фон: звёзды и каркасный рельеф внизу экрана. */
export function Terrain() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const redraw = () => draw(c);
    redraw();
    window.addEventListener('resize', redraw);
    return () => window.removeEventListener('resize', redraw);
  }, []);
  return (
    <div className={s.backdrop} aria-hidden="true">
      <canvas ref={ref} className={s.canvas} />
    </div>
  );
}
