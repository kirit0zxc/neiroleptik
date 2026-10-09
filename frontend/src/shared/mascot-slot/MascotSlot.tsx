import s from './mascot.module.css';

/**
 * Место под маскота «Юми» (specs/frontend.md, раздел 9).
 *
 * Пока маскота нет, в левой колонке стоит окно-заглушка с портретом,
 * а под основным листом — консоль, куда Юми будет печатать разбор атак.
 */
export function MascotSlot() {
  return (
    <section className={`win ${s.slot}`} data-mascot-slot aria-labelledby="yumi-title">
      <div className="win-titlebar"><span className="win-icon" aria-hidden="true" /><h2 id="yumi-title">Юми</h2></div>
      <div className={s.portrait} aria-hidden="true"><span className={s.corners} /></div>
      <p className={s.caption}>Юми скоро подключится</p>
    </section>
  );
}

/** Консоль Юми под основным листом: пока пустая, без выдуманных разборов. */
export function YumiConsole() {
  return (
    <section className={s.console} aria-labelledby="yumi-console-title">
      <div className={s.consoleHead}><h2 id="yumi-console-title">Юми › анализ</h2></div>
      <p className={s.consoleBody}>
        Здесь Юми будет разбирать атаки: стадию, что произошло и что проверить дальше.
        <span className={s.cursor} aria-hidden="true" />
      </p>
    </section>
  );
}
