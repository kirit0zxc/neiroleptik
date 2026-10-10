/**
 * Точка входа для сборки одним файлом — для content script расширения
 * и для любой страницы, куда достаточно подключить один скрипт.
 * Внутри всё, кроме React.
 *
 *   <script src="mascot.global.js"></script>
 *   Mascot.mount(document.getElementById('yumi'), { assetsBase: '/mascot/' });
 *   Mascot.connect().listenWebSocket(ws);
 */
export * from './index';
export * from './vrm';
