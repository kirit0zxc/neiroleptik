import { createContext, useContext, useSyncExternalStore } from 'react';
import { MascotBridge } from '../../mascot/bridge/MascotBridge';
import type { MascotCore } from '../../mascot/core/MascotCore';
import { getMascot } from '../../mascot/singleton';
import { YUMI_RULES } from './rules';

/** Реплика Юми в консоли. */
export interface YumiLine { id: number; at: Date; text: string }

/** Сколько реплик видно в консоли. */
const KEEP = 5;
/** Окно схлопывания событий атаки, мс: последнее в окне всегда доходит. */
const ATTACK_WINDOW_MS = 1500;

/**
 * Юми в HoneyForge (specs/frontend.md, раздел 9): ядро без звука, мост с
 * таблицей реакций продукта и лог реплик для консоли. Экраны сообщают, что
 * произошло, через handle(); события атаки идут через attack() и
 * схлопываются, чтобы шторм подключений не заглушал ответы на команды.
 */
export class Yumi {
  readonly core: MascotCore;
  private readonly bridge: MascotBridge;
  private lines: readonly YumiLine[] = [];
  private nextId = 1;
  private readonly listeners = new Set<() => void>();
  private attackTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingAttack: { event: string; payload: unknown } | null = null;
  private readonly offSay: () => void;

  constructor(core: MascotCore = getMascot()) {
    this.core = core;
    // Голос — отдельный этап: реплики только текстом в консоли.
    core.setMuted(true);
    this.bridge = new MascotBridge(core, YUMI_RULES, { fallback: null });
    this.offSay = core.on((e) => { if (e.type === 'say') this.push(e.text); });
  }

  /** Событие интерфейса или центра: реакцию выберет таблица. */
  handle(event: string, payload?: unknown) {
    this.bridge.handle(event, payload);
  }

  /** Событие атаки: не чаще раза в окно, последнее в окне доходит. */
  attack(event: string, payload: unknown) {
    if (this.attackTimer) {
      this.pendingAttack = { event, payload };
      return;
    }
    this.handle(event, payload);
    this.attackTimer = setTimeout(() => this.flushAttack(), ATTACK_WINDOW_MS);
  }

  getLines = (): readonly YumiLine[] => this.lines;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  dispose() {
    this.offSay();
    this.bridge.dispose();
    if (this.attackTimer) clearTimeout(this.attackTimer);
    this.attackTimer = null;
    this.pendingAttack = null;
  }

  private flushAttack() {
    this.attackTimer = null;
    const next = this.pendingAttack;
    this.pendingAttack = null;
    if (next) this.attack(next.event, next.payload);
  }

  private push(text: string) {
    this.lines = [...this.lines, { id: this.nextId++, at: new Date(), text }].slice(-KEEP);
    for (const l of this.listeners) l();
  }
}

export const YumiContext = createContext<Yumi | null>(null);

/** Юми приложения или null (вне YumiContext — например, в отдельных тестах экранов). */
export function useYumi(): Yumi | null {
  return useContext(YumiContext);
}

const NO_LINES: readonly YumiLine[] = [];
const noop = () => () => {};

/** Реплики для консоли. */
export function useYumiLines(): readonly YumiLine[] {
  const yumi = useYumi();
  return useSyncExternalStore(yumi?.subscribe ?? noop, yumi ? yumi.getLines : () => NO_LINES);
}
