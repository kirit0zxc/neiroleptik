import { useEffect, useRef, useState } from 'react';

/** Пункт меню окна. */
export interface MenuItem {
  label: string;
  onSelect: () => void;
  /** Галочка слева: для переключателей и выбора одного из нескольких. */
  checked?: boolean;
  disabled?: boolean;
}

/** Раздел меню: «Вид», «Звук»… null в items — разделитель. */
export interface MenuSection {
  label: string;
  items: (MenuItem | null)[];
}

/**
 * Строка меню в духе старых окон: раздел открывается кликом, пока
 * один открыт — соседние открываются наведением. Закрывается кликом
 * мимо, Escape или выбором пункта.
 *
 * С клавиатуры: ←/→ — между разделами, ↓/Enter — открыть и перейти к
 * пунктам, ↑/↓ — по пунктам, Escape — закрыть и вернуть фокус разделу.
 */
export function MenuBar({ sections }: { sections: MenuSection[] }) {
  const [open, setOpen] = useState(-1);
  const bar = useRef<HTMLDivElement>(null);
  const tops = useRef<(HTMLButtonElement | null)[]>([]);
  /** После открытия с клавиатуры — перевести фокус на первый пункт. */
  const focusFirst = useRef(false);

  /** Доступные пункты открытого раздела. */
  function items(i: number) {
    return [...(bar.current?.querySelectorAll<HTMLButtonElement>(`[data-menu="${i}"] .yumi-item:not(:disabled)`) ?? [])];
  }

  useEffect(() => {
    if (open < 0) return;
    // Документ берём у самой строки меню: окно может быть в Picture-in-Picture.
    const doc = bar.current?.ownerDocument ?? document;
    const onDown = (e: PointerEvent) => {
      if (!bar.current?.contains(e.target as Node)) setOpen(-1);
    };
    doc.addEventListener('pointerdown', onDown);
    return () => doc.removeEventListener('pointerdown', onDown);
  }, [open]);

  useEffect(() => {
    if (open < 0 || !focusFirst.current) return;
    focusFirst.current = false;
    items(open)[0]?.focus();
  }, [open]);

  const close = (i: number) => { setOpen(-1); tops.current[i]?.focus(); };

  const onTopKey = (e: React.KeyboardEvent, i: number) => {
    const n = sections.length;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      const next = (i + (e.key === 'ArrowRight' ? 1 : -1) + n) % n;
      tops.current[next]?.focus();
      if (open >= 0) setOpen(next);
    } else if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      focusFirst.current = true;
      if (open === i) items(i)[0]?.focus();
      else setOpen(i);
    } else if (e.key === 'Escape' && open >= 0) {
      close(i);
    }
  };

  const onItemKey = (e: React.KeyboardEvent, i: number) => {
    const list = items(i);
    const at = list.indexOf(e.target as HTMLButtonElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const next = (at + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length;
      list[next]?.focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close(i);
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      onTopKey(e, i);
    }
  };

  if (!sections.length) return null;

  return (
    <div className="yumi-menubar" ref={bar} role="menubar">
      {sections.map((section, i) => (
        <div key={section.label} className="yumi-menu" data-menu={i}>
          <button
            ref={(el) => { tops.current[i] = el; }}
            className={i === open ? 'yumi-menu-btn yumi-menu-btn--open' : 'yumi-menu-btn'}
            role="menuitem"
            aria-haspopup="menu"
            aria-expanded={i === open}
            onClick={() => setOpen(i === open ? -1 : i)}
            onPointerEnter={() => { if (open >= 0) setOpen(i); }}
            onKeyDown={(e) => onTopKey(e, i)}
          >
            {section.label}
          </button>
          {i === open && (
            <div className="yumi-dropdown" role="menu" aria-label={section.label}>
              {section.items.map((item, j) => (item === null
                ? <div key={`sep-${j}`} className="yumi-sep" role="separator" />
                : (
                  <button
                    key={item.label}
                    className="yumi-item"
                    role={item.checked === undefined ? 'menuitem' : 'menuitemcheckbox'}
                    aria-checked={item.checked}
                    disabled={item.disabled}
                    onClick={() => { close(i); item.onSelect(); }}
                    onKeyDown={(e) => onItemKey(e, i)}
                  >
                    <span className="yumi-check" aria-hidden="true">{item.checked ? '✓' : ''}</span>
                    {item.label}
                  </button>
                )))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
