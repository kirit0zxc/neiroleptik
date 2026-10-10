import { useEffect, useRef, type CSSProperties } from 'react';
import type { MascotCore } from '../core/MascotCore';
import type { VrmRenderer, VrmRendererOptions } from '../renderers/vrm/VrmRenderer';
import { getMascot } from '../singleton';

export interface MascotProps {
  /** По умолчанию берётся общий экземпляр, см. getMascot(). */
  core?: MascotCore;
  renderer?: VrmRendererOptions;
  className?: string;
  style?: CSSProperties;
}

/**
 * three.js и VRM весят сотни килобайт и нужны только для картинки.
 * Грузим их отдельным чанком, чтобы маскот не задерживал первую
 * отрисовку приложения, в которое его встроили.
 */
const loadRenderer = () => import('../renderers/vrm/VrmRenderer');

export function Mascot({ core, renderer, className, style }: MascotProps) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const instance = core ?? getMascot();
    let view: VrmRenderer | null = null;
    let cancelled = false;

    void loadRenderer().then(
      ({ VrmRenderer }) => {
        if (cancelled) return;
        view = new VrmRenderer(renderer);
        void instance.attach(view, host);
      },
      (error) => console.warn('[mascot] не удалось загрузить рендерер', error),
    );

    // Ядро само разберётся, был ли этот рендерер ещё подключён
    // или его уже сменил следующий монтаж.
    return () => {
      cancelled = true;
      if (view) instance.detach(view);
    };
    // Пересоздаём сцену только при смене ядра — опции читаются один раз.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [core]);

  return <div ref={hostRef} className={className} style={{ position: 'relative', ...style }} />;
}
