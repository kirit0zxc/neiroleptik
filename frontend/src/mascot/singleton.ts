import { MascotCore, type MascotCoreOptions } from './core/MascotCore';

let instance: MascotCore | null = null;

/**
 * Общий экземпляр маскота.
 *
 * Нужен, чтобы дёргать Юми откуда угодно — из интерсептора axios,
 * из обработчика WebSocket, из любого модуля, — не протаскивая
 * пропсы через половину дерева компонентов.
 *
 * Опции применяются только при первом вызове: дальше экземпляр уже
 * создан, и молча проглотить новые настройки было бы хуже, чем сказать.
 */
export function getMascot(options?: MascotCoreOptions): MascotCore {
  if (!instance) {
    instance = new MascotCore(options);
  } else if (options) {
    console.warn(
      '[mascot] getMascot(options): экземпляр уже создан, опции проигнорированы. ' +
      'Передайте их в самый первый вызов getMascot() или connect().',
    );
  }
  return instance;
}

export function resetMascot() {
  instance?.dispose();
  instance = null;
}
