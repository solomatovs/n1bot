import { ChevronDown, ChevronRight } from "lucide-react";
import type { ReactElement, ReactNode } from "react";
import { Link } from "react-router-dom";

import "./ItemRow.css";

import { StatusDot } from "./StatusDot";

type Toggle = {
  expanded: boolean;
  label: string;
  onToggle: () => void;
};

type Props = {
  /** Адрес перехода; без него строка — контейнер (workflow с кнопкой-стрелкой). */
  href?: string | undefined;
  selected?: boolean;
  /** Под-строка развёрнутой истории: отступ под родителя. */
  sub?: boolean;
  /** Статус запуска для точки. */
  status?: string | undefined;
  toggle?: Toggle | undefined;
  name?: ReactNode;
  pills?: ReactNode;
  meta?: ReactNode;
  onClick?: (() => void) | undefined;
};

/** Строка списка: точка статуса, стрелка разворота, имя, пилюли, мета.
 * Единственное место, где существует класс `item`. */
export function ItemRow({
  href,
  selected = false,
  sub = false,
  status,
  toggle,
  name,
  pills,
  meta,
  onClick,
}: Props): ReactElement {
  const classes = ["item"];
  if (sub) {
    classes.push("item--sub");
  }
  if (selected) {
    classes.push("item--on");
  }

  const dot = status !== undefined && <StatusDot status={status} />;
  const body = (
    <>
      {dot}
      {name !== undefined && <span className="item__name">{name}</span>}
      {pills !== undefined && <span className="item__pills">{pills}</span>}
      {meta !== undefined && <span className="item__meta">{meta}</span>}
    </>
  );

  if (href !== undefined && toggle === undefined) {
    return (
      <Link to={href} className={classes.join(" ")} data-status={status} onClick={onClick}>
        {body}
      </Link>
    );
  }

  return (
    <div className={classes.join(" ")} data-status={status}>
      {toggle !== undefined && (
        <button
          type="button"
          className="item__toggle"
          aria-label={toggle.label}
          aria-expanded={toggle.expanded}
          onClick={toggle.onToggle}
        >
          {toggle.expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </button>
      )}
      {href !== undefined ? (
        <Link to={href} className="item__body" onClick={onClick}>
          {body}
        </Link>
      ) : (
        body
      )}
    </div>
  );
}
