/**
 * Стили окна Юми.
 *
 * Модуль вставляет их сам, а не через импорт .css: окно может жить в
 * документе Picture-in-Picture, куда стили страницы не попадают, и в
 * приложении, где сборка CSS устроена по-своему.
 *
 * Цвета и размеры — CSS-переменные: приложение может перекрасить окно,
 * переопределив их на .yumi-window.
 */
const CSS = `
.yumi-window, .yumi-tab, .yumi-dock {
  --yumi-border: #9b6b9e;
  --yumi-border-dark: #7b4f80;
  --yumi-title: #dfa3d9;
  --yumi-title-text: #7a4a86;
  --yumi-menu: #f1a6c2;
  --yumi-menu-hover: #e58bb0;
  --yumi-body: #ffffff;
  --yumi-accent: #f6c1d3;
  --yumi-font: "Pixelify Sans", "Press Start 2P", ui-monospace, monospace;
  --yumi-width: clamp(210px, 22vw, 340px);
  --yumi-z: 2147483000;
  font-family: var(--yumi-font);
  box-sizing: border-box;
}
.yumi-window *, .yumi-dock * { box-sizing: border-box; }

/* ─── Рамка ─────────────────────────────────────────────── */

.yumi-window {
  position: fixed;
  z-index: var(--yumi-z);
  width: var(--yumi-width);
  display: flex; flex-direction: column;
  border: 3px solid var(--yumi-border);
  background: var(--yumi-menu);
  /* Ступенчатая «пиксельная» тень вместо мягкой. */
  box-shadow: 3px 3px 0 var(--yumi-border-dark), 6px 6px 0 rgba(123, 79, 128, .25);
  user-select: none;
  -webkit-user-select: none;
  touch-action: none;
}
.yumi-window--pip {
  position: static;
  width: 100vw; height: 100vh;
  box-shadow: none;
}

.yumi-titlebar {
  display: flex; align-items: center; gap: 6px;
  height: 30px;
  padding: 0 4px 0 6px;
  background: var(--yumi-title);
  border-bottom: 3px solid var(--yumi-border);
  color: var(--yumi-title-text);
  cursor: grab;
}
.yumi-window--dragging .yumi-titlebar { cursor: grabbing; }
.yumi-window--pip .yumi-titlebar { cursor: default; }

.yumi-icon {
  flex: 0 0 18px; height: 14px;
  border: 2px solid var(--yumi-title-text);
  border-radius: 2px;
  background: var(--yumi-accent);
  box-shadow: inset 0 -3px 0 var(--yumi-title-text);
}
.yumi-title {
  flex: 1; min-width: 0;
  font-size: 17px; font-weight: 600; letter-spacing: .02em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.yumi-tbtn {
  flex: 0 0 24px; height: 22px;
  display: grid; place-items: center;
  padding: 0;
  border: 0; background: none;
  color: var(--yumi-title-text);
  font: inherit; font-size: 18px; line-height: 1;
  cursor: pointer;
}
.yumi-tbtn:hover { background: rgba(255, 255, 255, .35); }
.yumi-tbtn:focus-visible, .yumi-menu-btn:focus-visible, .yumi-item:focus-visible,
.yumi-tab:focus-visible, .yumi-stage:focus-visible { outline: 2px dashed var(--yumi-title-text); outline-offset: -2px; }
.yumi-tbtn:disabled { opacity: .35; cursor: default; background: none; }

/* Пиктограммы кнопок нарисованы рамками — чёткие на любом масштабе. */
.yumi-ico-min { width: 12px; height: 4px; background: var(--yumi-accent); border: 2px solid var(--yumi-title-text); }
.yumi-ico-max {
  width: 14px; height: 13px;
  border: 2px solid var(--yumi-title-text);
  background: linear-gradient(var(--yumi-accent), var(--yumi-accent)) 70% 30% / 5px 5px no-repeat;
}
.yumi-ico-close { font-size: 20px; font-weight: 600; margin-top: -2px; }

/* ─── Меню ──────────────────────────────────────────────── */

.yumi-menubar {
  position: relative;
  display: flex; gap: 2px;
  padding: 2px 4px;
  background: var(--yumi-menu);
  border-bottom: 3px solid var(--yumi-border);
}
.yumi-menu { position: relative; }
.yumi-menu-btn {
  padding: 1px 7px;
  border: 0; background: none;
  color: var(--yumi-title-text);
  font: inherit; font-size: 16px;
  cursor: pointer;
}
.yumi-menu-btn:hover, .yumi-menu-btn--open { background: var(--yumi-menu-hover); color: #fff; }

.yumi-dropdown {
  position: absolute; left: 0; top: calc(100% + 2px);
  z-index: 2;
  min-width: 170px;
  padding: 3px;
  border: 3px solid var(--yumi-border);
  background: var(--yumi-body);
  box-shadow: 3px 3px 0 var(--yumi-border-dark);
}
.yumi-item {
  display: flex; align-items: center; gap: 6px;
  width: 100%;
  padding: 3px 8px 3px 4px;
  border: 0; background: none;
  color: var(--yumi-title-text);
  font: inherit; font-size: 15px; text-align: left;
  cursor: pointer;
}
.yumi-item:hover:not(:disabled) { background: var(--yumi-menu); }
.yumi-item:disabled { opacity: .4; cursor: default; }
.yumi-check { flex: 0 0 14px; text-align: center; }
.yumi-sep { height: 2px; margin: 3px 2px; background: var(--yumi-accent); }

/* ─── Сцена и реплика ───────────────────────────────────── */

.yumi-body {
  position: relative;
  margin: 4px;
  border: 3px solid var(--yumi-border);
  background: var(--yumi-body);
  /* Почти квадрат: по пояс, без пустого низа. */
  aspect-ratio: 1 / 1.04;
  overflow: hidden;
}
.yumi-window--pip .yumi-body { flex: 1; aspect-ratio: auto; }
.yumi-stage { position: absolute; inset: 0; cursor: pointer; }

/* ─── Фон сцены ─────────────────────────────────────────── */

.yumi-body {
  /* Где на сцене центр фона (туннеля) и во сколько раз фон больше сцены. */
  --yumi-bg-x: 50%;
  --yumi-bg-y: 36%;
  --yumi-bg-size: 100%;
  /* Пропорции картинки фона. */
  --yumi-bg-ratio: 4 / 3;
  background-color: #0b0626;
}
/* Фон крупнее сцены и центрирован в точке (--yumi-bg-x, --yumi-bg-y):
   так центр картинки встаёт за голову Юми, а края не оголяются. */
.yumi-backdrop {
  position: absolute;
  left: var(--yumi-bg-x); top: var(--yumi-bg-y);
  width: var(--yumi-bg-size);
  aspect-ratio: var(--yumi-bg-ratio);
  /* Не меньше, чем нужно, чтобы фон доставал до низа сцены (центр — в верхней половине). */
  min-height: calc(2 * (100% - var(--yumi-bg-y)));
  transform: translate(-50%, -50%);
  pointer-events: none;
  background: center / cover no-repeat;
}

.yumi-bubble {
  position: absolute; left: 6px; right: 6px; bottom: 6px;
  padding: 6px 9px;
  border: 3px solid var(--yumi-border);
  background: var(--yumi-body);
  box-shadow: 3px 3px 0 var(--yumi-accent);
  color: var(--yumi-title-text);
  font-size: 15px; line-height: 1.25;
  animation: yumi-pop .18s steps(3) both;
  pointer-events: none;
}
@keyframes yumi-pop { from { transform: translateY(6px); opacity: 0; } to { transform: none; opacity: 1; } }

.yumi-placeholder {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  padding: 12px;
  color: var(--yumi-title-text);
  font-size: 15px; text-align: center;
}

/* ─── Вкладка свёрнутого окна ───────────────────────────── */

.yumi-dock {
  position: fixed; left: 0;
  z-index: var(--yumi-z);
  display: flex; align-items: flex-start; gap: 8px;
  pointer-events: none;
}
.yumi-note {
  max-width: min(260px, 60vw);
  padding: 6px 9px;
  border: 3px solid var(--yumi-border);
  background: var(--yumi-body);
  box-shadow: 3px 3px 0 var(--yumi-accent);
  color: var(--yumi-title-text);
  font-size: 15px; line-height: 1.25;
  animation: yumi-pop .18s steps(3) both;
}
.yumi-tab {
  pointer-events: auto;
  display: flex; align-items: center; gap: 6px;
  padding: 6px 10px 6px 8px;
  border: 3px solid var(--yumi-border);
  border-left: 0;
  background: var(--yumi-title);
  box-shadow: 3px 3px 0 var(--yumi-border-dark);
  color: var(--yumi-title-text);
  font-size: 16px; font-weight: 600;
  cursor: pointer;
}
.yumi-tab:hover { background: var(--yumi-menu); }
.yumi-tab--slim { padding: 10px 4px; }

@media (max-width: 600px) {
  .yumi-window { --yumi-width: min(64vw, 250px); }
  .yumi-title { font-size: 15px; }
  .yumi-menu-btn { font-size: 14px; padding: 1px 5px; }
}

@media (prefers-reduced-motion: reduce) {
  .yumi-bubble, .yumi-note { animation: none; }
}
`;

const STYLE_ID = 'yumi-window-styles';
const FONT_ID = 'yumi-window-font';
const FONT_URL = 'https://fonts.googleapis.com/css2?family=Pixelify+Sans:wght@400;600&display=swap';

/**
 * Вставить стили окна в документ — один раз. font: false — не тянуть
 * шрифт с Google Fonts (свой шрифт задаётся через --yumi-font).
 */
export function ensureWindowStyles(doc: Document, font = true, nonce?: string) {
  if (!doc.getElementById(STYLE_ID)) {
    const style = doc.createElement('style');
    style.id = STYLE_ID;
    // CSP со style-src 'nonce-…' пропустит только помеченный стиль.
    if (nonce) style.nonce = nonce;
    style.textContent = CSS;
    doc.head.appendChild(style);
  }
  if (font && !doc.getElementById(FONT_ID)) {
    const link = doc.createElement('link');
    link.id = FONT_ID;
    link.rel = 'stylesheet';
    link.href = FONT_URL;
    doc.head.appendChild(link);
  }
}
