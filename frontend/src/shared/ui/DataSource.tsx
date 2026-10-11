import { StatusBadge } from '.';
import s from './ui.module.css';

/**
 * Источник данных интерфейса (specs/frontend.md, «Источник данных: демо или
 * центр»). Режим задаётся сборкой: VITE_API_MOCK=1 — моки MSW.
 */
export const isDemo = () => import.meta.env.VITE_API_MOCK === '1';

export const DEMO_TITLE = 'Данные вымышленные (моки), не с центра и не с ловушек';

/**
 * Метка в шапке: «демо-данные» или «центр» — текстом и цветом. Сообщает
 * источник (режим сборки), а не доступность центра: «центр» нейтральный,
 * чтобы не читался как «связь есть» — о связи говорит статус потока.
 */
export function DataSourceBadge() {
  // Пояснение — скрытым текстом, а не aria-label: на span без роли его читают не все читалки.
  return isDemo()
    ? <span title={DEMO_TITLE}><span className={s.srOnly}>Источник данных: </span><StatusBadge tone="warn">демо-данные</StatusBadge><span className={s.srOnly}>. {DEMO_TITLE}.</span></span>
    : <span title="Данные с центра HoneyForge"><span className={s.srOnly}>Источник данных: </span><StatusBadge tone="idle">центр</StatusBadge><span className={s.srOnly}> HoneyForge.</span></span>;
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
