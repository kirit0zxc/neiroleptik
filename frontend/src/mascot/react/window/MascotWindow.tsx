import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { MascotCore } from '../../core/MascotCore';
import type { VrmRenderer, VrmRendererOptions } from '../../renderers/vrm/VrmRenderer';
import { getMascot } from '../../singleton';
import { Mascot } from '../Mascot';
import { useMascot } from '../useMascot';
import { MenuBar, type MenuSection } from './MenuBar';
import { PipWindow } from './pip';
import { WindowPlacement, type Placement, type WindowFit } from './placement';
import { ensureWindowStyles } from './styles';

export interface MascotWindowProps {
  /** По умолчанию — общий экземпляр, см. getMascot(). */
  core?: MascotCore;
  /** Подпись в заголовке. */
  title?: string;
  /**
   * Меню. Массив заменяет стандартное, функция получает стандартное
   * и возвращает своё — так удобно добавить раздел, не теряя готовых.
   */
  menu?: MenuSection[] | ((defaults: MenuSection[]) => MenuSection[]);
  renderer?: VrmRendererOptions;
  /** Ключ, под которым запоминается положение окна. */
  storageKey?: string;
  /**
   * Пиксельный шрифт с Google Fonts. false — не грузить (свой шрифт
   * задаётся через --yumi-font): нужно при строгой CSP или если
   * приложение не хочет внешних запросов.
   */
  font?: boolean;
  /** nonce для вставляемого <style> — при CSP со style-src 'nonce-…'. */
  nonce?: string;
  /**
   * Фон сцены. По умолчанию — комната с экранами (bg/room.webp в папке
   * ассетов), строка — свой адрес картинки, false — без фона.
   */
  backdrop?: boolean | string;
  /** Клик по Юми. По умолчанию она здоровается. */
  onMascotClick?: (core: MascotCore) => void;
}

/** Размер до первого замера — чтобы стартовое положение было разумным. */
const GUESS = { width: 300, height: 380 };

const viewportOf = (win: Window) => ({ width: win.innerWidth, height: win.innerHeight });

/**
 * Юми в ретро-окне поверх интерфейса.
 *
 * Заголовок перетаскивается; «–» сворачивает во вкладку у левого края,
 * «□» выносит в отдельное окно поверх всех окон (Chromium), «X» прячет
 * до узкой вкладки. Положение, режим, кадр и «без звука» запоминаются.
 *
 * Окно рендерится порталом в document.body: transform или filter у
 * предков в приложении не сдвинут и не обрежут его.
 */
export function MascotWindow({
  core: coreProp,
  title = 'yumi',
  menu,
  renderer,
  storageKey,
  font = true,
  nonce,
  backdrop = true,
  onMascotClick,
}: MascotWindowProps) {
  const core = useMemo(() => coreProp ?? getMascot(), [coreProp]);
  const { bubble, muted } = useMascot(core);
  const store = useMemo(() => new WindowPlacement(storageKey), [storageKey]);

  const [place, setPlace] = useState<Placement>(() => store.load(viewportOf(window), GUESS));
  const [viewport, setViewport] = useState(() => viewportOf(window));
  const [measured, setMeasured] = useState(GUESS);
  const [pipWin, setPipWin] = useState<Window | null>(null);
  const [dragging, setDragging] = useState(false);
  const frame = useRef<HTMLDivElement>(null);
  const pip = useMemo(() => new PipWindow(() => setPipWin(null)), []);

  useLayoutEffect(() => { ensureWindowStyles(document, font, nonce); }, [font, nonce]);

  // «Без звука» живёт в ядре — один источник правды. Окно лишь
  // восстанавливает выбор пользователя при появлении и запоминает его.
  useEffect(() => { core.setMuted(place.muted); }, [core]); // eslint-disable-line react-hooks/exhaustive-deps

  // Закрытое окно — Юми молчит. Свёрнутое — говорит, а реплика
  // показывается уведомлением у вкладки.
  const closed = place.mode === 'closed' && !pipWin;
  useEffect(() => {
    core.setVoiceSuppressed(closed);
    return () => core.setVoiceSuppressed(false);
  }, [core, closed]);

  // Запоминаем по окончании перетаскивания, а не на каждое движение мыши.
  useEffect(() => {
    if (!dragging) store.save({ ...place, muted });
  }, [store, place, muted, dragging]);

  // Размер экрана и окна — для отрисовки в пределах экрана.
  useEffect(() => {
    const onResize = () => setViewport(viewportOf(window));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  // Размер рамки меняется с шириной экрана (CSS) и после загрузки шрифта.
  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setMeasured((m) => (Math.abs(r.width - m.width) > 1 || Math.abs(r.height - m.height) > 1
        ? { width: r.width, height: r.height } : m));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [place.mode, pipWin]);

  // При закрытии страницы отдельное окно тоже закрываем.
  useEffect(() => () => pip.close(), [pip]);

  // Желаемое место хранится как есть; на экран окно ставится в его пределах.
  const shown = store.clamp(place, viewport, measured);

  // ─── Перетаскивание за заголовок ──────────────────────────

  const drag = useRef<{ dx: number; dy: number; id: number } | null>(null);

  const onTitleDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (pipWin || e.button !== 0 || (e.target as HTMLElement).closest('button')) return;
    drag.current = { dx: e.clientX - shown.x, dy: e.clientY - shown.y, id: e.pointerId };
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(true);
  };
  const onTitleMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    const next = store.clamp({ x: e.clientX - d.dx, y: e.clientY - d.dy }, viewport, measured);
    setPlace((p) => ({ ...p, ...next }));
  };
  const onTitleUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (drag.current?.id !== e.pointerId) return;
    drag.current = null;
    setDragging(false);
  };

  // ─── Действия ─────────────────────────────────────────────

  const setMode = (mode: Placement['mode']) => setPlace((p) => ({ ...p, mode }));

  const openPip = async () => {
    try {
      const win = await pip.open({ width: measured.width, height: measured.height }, font, nonce);
      if (win) setPipWin(win);
    } catch (error) {
      // Браузер отказал (не из клика, политика сайта) — остаёмся на странице.
      console.warn('[mascot] отдельное окно не открылось', error);
    }
  };

  const view = useCallback(() => core.getRenderer() as VrmRenderer | null, [core]);
  const setFit = (fit: WindowFit) => {
    view()?.setCamera?.({ fit });
    setPlace((p) => ({ ...p, fit }));
  };
  // Выбор из меню переживает сворачивание и вынос в окно: новый
  // рендерер создаётся сразу с ним.
  const rendererOptions = useMemo(
    () => (place.fit ? { ...renderer, fit: place.fit } : renderer),
    [renderer, place.fit],
  );
  const currentFit = place.fit ?? view()?.getCamera?.().fit;

  const defaults: MenuSection[] = [
    {
      label: 'Вид',
      items: [
        { label: 'Во весь рост', checked: currentFit === 'full', onSelect: () => setFit('full') },
        { label: 'По пояс', checked: currentFit === 'half', onSelect: () => setFit('half') },
        { label: 'Портрет', checked: currentFit === 'portrait', onSelect: () => setFit('portrait') },
        ...(PipWindow.supported() && !pipWin
          ? [null, { label: 'В отдельное окно', onSelect: () => void openPip() }]
          : []),
      ],
    },
    {
      label: 'Звук',
      items: [
        { label: 'Говорить вслух', checked: !muted, onSelect: () => core.setMuted(!muted) },
      ],
    },
    {
      label: 'Юми',
      items: [
        { label: 'Поздороваться', onSelect: () => core.setState('welcome') },
        { label: 'Станцевать', onSelect: () => core.setState('dance') },
        null,
        { label: 'Свернуть', onSelect: () => { pip.close(); setMode('minimized'); } },
      ],
    },
  ];
  // menu() только собирает пункты; обработчики вызываются позже, по клику.
  const sections = typeof menu === 'function' ? menu(defaults) : menu ?? defaults;

  const backdropUrl = typeof backdrop === 'string' ? backdrop
    : backdrop ? `${rendererOptions?.assetsBase ?? '/mascot/'}bg/room.webp` : null;

  const clickMascot = () => (onMascotClick ? onMascotClick(core) : core.setState('welcome'));

  // ─── Разметка ─────────────────────────────────────────────

  const body = (inPip: boolean) => (
    <div
      ref={frame}
      className={[
        'yumi-window',
        inPip && 'yumi-window--pip',
        dragging && 'yumi-window--dragging',
      ].filter(Boolean).join(' ')}
      style={inPip ? undefined : { left: shown.x, top: shown.y }}
      role="complementary"
      aria-label={`Юми — ${title}`}
    >
      <div
        className="yumi-titlebar"
        onPointerDown={onTitleDown}
        onPointerMove={onTitleMove}
        onPointerUp={onTitleUp}
        onPointerCancel={onTitleUp}
      >
        <span className="yumi-icon" aria-hidden="true" />
        <span className="yumi-title">{title}</span>
        {!inPip && (
          <button className="yumi-tbtn" onClick={() => setMode('minimized')} aria-label="Свернуть" title="Свернуть">
            <span className="yumi-ico-min" />
          </button>
        )}
        <button
          className="yumi-tbtn"
          onClick={() => (inPip ? pip.close() : void openPip())}
          disabled={!inPip && !PipWindow.supported()}
          aria-label={inPip ? 'Вернуть на страницу' : 'В отдельное окно'}
          title={inPip ? 'Вернуть на страницу'
            : PipWindow.supported() ? 'В отдельное окно поверх всех' : 'Отдельное окно есть только в Chrome и Edge'}
        >
          <span className="yumi-ico-max" />
        </button>
        <button
          className="yumi-tbtn"
          onClick={() => { pip.close(); setMode('closed'); }}
          aria-label="Закрыть"
          title="Закрыть"
        >
          <span className="yumi-ico-close">×</span>
        </button>
      </div>

      <MenuBar sections={sections} />

      <div className="yumi-body">
        {backdropUrl && (
          <div className="yumi-backdrop" aria-hidden="true" style={{ backgroundImage: `url("${backdropUrl}")` }} />
        )}
        <div
          className="yumi-stage"
          role="button"
          tabIndex={0}
          aria-label="Юми. Нажмите, чтобы она отреагировала"
          onClick={clickMascot}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            e.preventDefault();
            clickMascot();
          }}
        >
          <Mascot core={core} renderer={rendererOptions} style={{ position: 'absolute', inset: 0 }} />
        </div>
        {bubble && <div className="yumi-bubble" role="status">{bubble}</div>}
      </div>
    </div>
  );

  /** Вкладка у левого края; у свёрнутой — уведомление с репликой. */
  const tab = (label: React.ReactNode, onClick: () => void, slim = false, hint = 'Показать Юми') => (
    <div className="yumi-dock" style={{ top: shown.y }}>
      <button
        className={slim ? 'yumi-tab yumi-tab--slim' : 'yumi-tab'}
        onClick={onClick}
        aria-label={hint}
        title={hint}
      >
        <span className="yumi-icon" aria-hidden="true" />
        {label}
      </button>
      {!slim && bubble && <div className="yumi-note" role="status">{bubble}</div>}
    </div>
  );

  let onPage: React.ReactNode;
  if (pipWin) onPage = tab('Юми в окне ↩', () => pip.close(), false, 'Вернуть Юми на страницу');
  else if (place.mode === 'minimized') onPage = tab(title, () => setMode('open'));
  else if (place.mode === 'closed') onPage = tab(null, () => setMode('open'), true);
  else onPage = body(false);

  return (
    <>
      {createPortal(onPage, document.body)}
      {pipWin && createPortal(body(true), pipWin.document.body)}
    </>
  );
}
