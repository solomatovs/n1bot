import { ChevronDown, ChevronRight } from "lucide-react";
import type { ReactElement, ReactNode } from "react";
import { Link } from "react-router-dom";

import "./ListRow.css";

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
  draft?: boolean;
  /** Статус запуска для точки; либо явный цвет токеном. */
  status?: string | undefined;
  dotColor?: string | undefined;
  toggle?: Toggle | undefined;
  name?: ReactNode;
  pills?: ReactNode;
  meta?: ReactNode;
  dataDraft?: string | undefined;
  onClick?: (() => void) | undefined;
};

/** Строка списка: точка статуса, стрелка разворота, имя, пилюли, мета.
 * Единственное место, где существует класс `item`. */
export function ListRow({
  href,
  selected = false,
  sub = false,
  draft = false,
  status,
  dotColor,
  toggle,
  name,
  pills,
  meta,
  dataDraft,
  onClick,
}: Props): ReactElement {
  const classes = ["item"];
  if (sub) {
    classes.push("item--sub");
  }
  if (draft) {
    classes.push("item--draft");
  }
  if (selected) {
    classes.push("item--on");
  }

  const dot = (status !== undefined || dotColor !== undefined) && (
    <StatusDot status={status} color={dotColor} />
  );
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
      <Link to={href} className={classes.join(" ")} data-status={status} data-draft={dataDraft} onClick={onClick}>
        {body}
      </Link>
    );
  }

  return (
    <div className={classes.join(" ")} data-status={status} data-draft={dataDraft}>
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
