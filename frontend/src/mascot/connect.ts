import { MascotBridge } from './bridge/MascotBridge';
import { DEFAULT_RULES } from './bridge/rules';
import type { BridgeOptions, BridgeRules } from './bridge/types';
import { getMascot } from './singleton';
import type { MascotCoreOptions } from './core/MascotCore';

export interface ConnectOptions extends BridgeOptions {
  /** Свои правила поверх стандартных. */
  rules?: BridgeRules;
  /** Не подмешивать стандартную таблицу. */
  replaceRules?: boolean;
  core?: MascotCoreOptions;
}

/**
 * Один вызов для подключения маскота к пайплайну.
 *
 *   const bridge = connect();
 *   bridge.listenWebSocket(ws);
 *   window.fetch = bridge.wrapFetch();
 *
 * Дальше бэкенд может слать события как привык: мост разберёт
 * сообщение, найдёт реакцию в таблице и ничего не уронит, если
 * придёт что-то неожиданное.
 */
export function connect(options: ConnectOptions = {}) {
  const { rules, replaceRules, core: coreOptions, ...bridgeOptions } = options;
  const core = getMascot(coreOptions);
  const table = replaceRules ? (rules ?? {}) : { ...DEFAULT_RULES, ...rules };
  return new MascotBridge(core, table, bridgeOptions);
}
