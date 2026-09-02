import type { ButtonHTMLAttributes, ReactElement, ReactNode, Ref } from "react";

import "./Menu.css";

/** Обвязка выпадающего меню: контейнер-позиционер, список, группа, пункт.
 * Поведение (открытие, клик мимо) остаётся у владельца меню.
 * Единственное место с классами семейства `menu`. */
export function Menu({ children, containerRef }: { children: ReactNode; containerRef?: Ref<HTMLDivElement> }): ReactElement {
  return (
    <div className="menu" ref={containerRef}>
      {children}
    </div>
  );
}

export function MenuList({ children, label }: { children: ReactNode; label: string }): ReactElement {
  return (
    <div className="menu__list" role="menu" aria-label={label}>
      {children}
    </div>
  );
}

export function MenuGroup({ children }: { children: ReactNode }): ReactElement {
  return <div className="menu__group eyebrow">{children}</div>;
}

type ItemProps = ButtonHTMLAttributes<HTMLButtonElement>;

export function MenuItem({ children, type, ...rest }: ItemProps): ReactElement {
  return (
    <button type={type ?? "button"} role="menuitem" className="menu__item" {...rest}>
      {children}
    </button>
  );
}
