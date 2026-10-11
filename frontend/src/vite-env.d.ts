/// <reference types="vite/client" />
interface ImportMetaEnv {
  /** 1 — работать на моках MSW без backend. */
  readonly VITE_API_MOCK?: string;
  /** 1 — общий выключатель телеметрии: включает события, аналитику и replay. */
  readonly VITE_TELEMETRY_ENABLED?: string;
  /** 1 — лента событий и живой WebSocket-поток готовы на сервере. */
  readonly VITE_EVENTS_ENABLED?: string;
  /** 1 — сводка обзора `/analytics/summary` готова на сервере. */
  readonly VITE_ANALYTICS_ENABLED?: string;
  /** 1 — запись сессии (replay) готова на сервере. */
  readonly VITE_REPLAY_ENABLED?: string;
  readonly VITE_INVESTIGATION_ENABLED?: string;
}
