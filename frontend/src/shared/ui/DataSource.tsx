import { StatusBadge } from '.';
import s from './ui.module.css';

/**
 * Источник данных интерфейса (specs/frontend.md, «Источник данных: демо или
 * центр»). Режим задаётся сборкой: VITE_API_MOCK=1 — моки MSW.
 */
export const isDemo = () => import.meta.env.VITE_API_MOCK === '1';

export const DEMO_TITLE = 'Данные вымышленные (моки), не с центра и не с ловушек';

/** Метка в шапке: «демо-данные» или «центр» — текстом и цветом. */
export function DataSourceBadge() {
  return isDemo()
    ? <span title={DEMO_TITLE} aria-label={`Источник данных: демо-данные. ${DEMO_TITLE}`}><StatusBadge tone="warn">демо-данные</StatusBadge></span>
    : <span title="Данные с центра HoneyForge" aria-label="Источник данных: центр HoneyForge"><StatusBadge tone="ok">центр</StatusBadge></span>;
}

/** Строка под шапкой и на входе — только в демо-режиме. */
export function DemoNote({ logins = false }: { logins?: boolean }) {
  if (!isDemo()) return null;
  return (
    <p className={s.demoNote} role="note">
      Демо-режим: данные вымышленные (моки), не с центра и не с ловушек.
      {logins && <> Входы: operator / operator, admin / admin, viewer / viewer.</>}
    </p>
  );
}
