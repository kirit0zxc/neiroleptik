import { ensureWindowStyles } from './styles';

/** Document Picture-in-Picture есть только в Chromium — описываем сами. */
interface DocumentPiP {
  requestWindow(options?: { width?: number; height?: number }): Promise<Window>;
  window: Window | null;
}

const api = (): DocumentPiP | undefined =>
  (globalThis as unknown as { documentPictureInPicture?: DocumentPiP }).documentPictureInPicture;

/**
 * Отдельное окно поверх всех окон для Юми.
 *
 * Окно живёт в том же JS-контексте, что и страница: ядро, состояние и
 * подписки общие, второй копии Юми нет. Открыть можно только из
 * обработчика клика — так требует браузер.
 */
export class PipWindow {
  private win: Window | null = null;
  private readonly onClosed: () => void;

  constructor(onClosed: () => void) {
    this.onClosed = onClosed;
  }

  static supported() { return api() !== undefined; }

  get window() { return this.win; }

  async open(size: { width: number; height: number }, font = true, nonce?: string): Promise<Window | null> {
    const pip = api();
    if (!pip) return null;
    if (this.win) return this.win;

    const win = await pip.requestWindow({ width: Math.round(size.width), height: Math.round(size.height) });
    ensureWindowStyles(win.document, font, nonce);
    win.document.title = 'Юми';
    // Окно без отступов и прокрутки — в нём только рамка с Юми.
    Object.assign(win.document.body.style, { margin: '0', overflow: 'hidden', background: '#f1a6c2' });
    // pagehide приходит и при закрытии крестиком системы, и при close().
    win.addEventListener('pagehide', () => {
      if (this.win !== win) return;
      this.win = null;
      this.onClosed();
    }, { once: true });
    this.win = win;
    return win;
  }

  close() {
    this.win?.close();
  }
}
