import { useEffect, useState, useSyncExternalStore } from 'react';
import { useYumi, useYumiLines } from '../../features/yumi/yumi';
import { Mascot } from '../../mascot/react/Mascot';
import { formatTime } from '../format/time';
import s from './mascot.module.css';

/** Окно Юми видно только на широком экране (раздел 6a) — иначе модель не грузим. */
const WIDE = '(min-width: 861px)';

function useMatchMedia(query: string): boolean {
  return useSyncExternalStore(
    (on) => {
      if (typeof window.matchMedia !== 'function') return () => {};
      const m = window.matchMedia(query);
      m.addEventListener('change', on);
      return () => m.removeEventListener('change', on);
    },
    () => typeof window.matchMedia === 'function' && window.matchMedia(query).matches,
  );
}

/** Контейнер модели растягивается на всю сцену (Mascot сам ставит position: relative). */
const FILL = { position: 'absolute', inset: 0 } as const;

let webglSupported: boolean | null = null;
/** Есть ли WebGL: без него 3D не нарисовать — остаётся портрет. */
function canWebGL(): boolean {
  if (webglSupported === null) {
    try {
      const canvas = document.createElement('canvas');
      webglSupported = !!(canvas.getContext('webgl2') ?? canvas.getContext('webgl'));
    } catch {
      webglSupported = false;
    }
  }
  return webglSupported;
}

/**
 * Окно «Юми» в левой колонке (specs/frontend.md, раздел 9): 3D-модель,
 * которая грузится лениво. До загрузки и при ошибке — портрет-заглушка.
 */
export function MascotSlot() {
  const yumi = useYumi();
  const wide = useMatchMedia(WIDE);
  const show3d = !!yumi && wide && canWebGL();
  const [phase, setPhase] = useState<'loading' | 'ready' | 'failed'>('loading');

  useEffect(() => {
    if (!yumi || !show3d) return;
    // Окно снова показано (например, экран расширили) — модель грузится заново.
    setPhase('loading');
    return yumi.core.on((e) => {
      if (e.type === 'ready') setPhase('ready');
      if (e.type === 'error') setPhase((p) => (p === 'ready' ? p : 'failed'));
    });
  }, [yumi, show3d]);

  // Готовая модель говорит сама за себя — подпись только для загрузки и отказа 3D.
  const caption = !show3d || phase === 'failed'
    ? '3D недоступно — реплики в консоли.'
    : phase === 'ready' ? null : 'Загружаю Юми…';

  return (
    <section className={`win ${s.slot}`} data-mascot-slot aria-labelledby="yumi-title">
      <div className="win-titlebar"><span className="win-icon" aria-hidden="true" /><h2 id="yumi-title">Юми</h2></div>
      <div className={s.stage} aria-hidden="true">
        {phase !== 'ready' && <div className={s.portrait}><span className={s.corners} /></div>}
        {show3d && phase !== 'failed' && <Mascot core={yumi.core} className={s.model} style={FILL} />}
      </div>
      {caption && <p className={s.caption}>{caption}</p>}
    </section>
  );
}

/** Время реплики в консоли: только часы, минуты и секунды — это события «сейчас». */
const CLOCK = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

/** Задержка между буквами при печати, мс. */
const TYPE_MS = 22;

function useReducedMotion(): boolean {
  return useMatchMedia('(prefers-reduced-motion: reduce)');
}

/** Печать последней реплики по буквам; при reduced motion — сразу целиком. */
function Typed({ text }: { text: string }) {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(reduced ? text.length : 0);
  useEffect(() => {
    if (reduced) { setShown(text.length); return; }
    setShown(0);
    const timer = setInterval(() => {
      setShown((n) => {
        if (n >= text.length) { clearInterval(timer); return n; }
        return n + 1;
      });
    }, TYPE_MS);
    return () => clearInterval(timer);
  }, [text, reduced]);
  return <>{text.slice(0, shown)}</>;
}

/**
 * Консоль Юми под основным листом: её реплики с временем, последние 5.
 * Печать по буквам скрыта от читалок; готовая строка объявляется один раз.
 */
export function YumiConsole() {
  const lines = useYumiLines();
  const last = lines.at(-1);
  return (
    <section className={s.console} aria-labelledby="yumi-console-title">
      <div className={s.consoleHead}><h2 id="yumi-console-title">Юми › анализ</h2></div>
      <div className={s.consoleBody} tabIndex={0} role='log' aria-label='Реплики Юми'>
        {!lines.length ? (
          <p className={s.line}>Здесь Юми рассказывает, что происходит с ловушками и атаками.<span className={s.cursor} aria-hidden="true" /></p>
        ) : lines.map((l) => (
          <p key={l.id} className={`${s.line} ${l === last ? '' : s.old}`} aria-hidden="true">
            <span className={s.time}>{formatTime(l.at.toISOString(), CLOCK)}</span>
            {l === last ? <Typed text={l.text} /> : l.text}
            {l === last && <span className={s.cursor} />}
          </p>
        ))}
        {/* Для читалок — готовая последняя реплика, без побуквенной печати. */}
        {/* key по id: одинаковая реплика подряд — новый узел, читалка объявит её снова. */}
        <p key={last?.id ?? 0} className={s.srOnly} aria-live="polite">{last?.text ?? ''}</p>
      </div>
    </section>
  );
}
