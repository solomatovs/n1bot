import type { ReactElement, ReactNode } from "react";

import { Eyebrow } from "./Eyebrow";
import "./Panel.css";

type PanelProps = {
  /** Текстовая сцена: ограничить ширину страницей. */
  page?: boolean;
  /** Без внутренних отступов: панель внутри секции другой панели. */
  flat?: boolean;
  mark?: string | undefined;
  children: ReactNode;
};

/** Содержимое панели деталей: шапка и секции.
 * Единственное место, где существуют классы `panel*` и `section*`. */
export function Panel({ page = false, flat = false, mark, children }: PanelProps): ReactElement {
  const classes = ["panel"];
  if (page) {
    classes.push("panel--page");
  }
  if (flat) {
    classes.push("panel--flat");
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {children}
    </div>
  );
}

type HeadProps = {
  eyebrow?: ReactNode;
  name: ReactNode;
  /** Имя моноширинным: адрес объекта. */
  mono?: boolean;
  icon?: ReactNode;
  actions?: ReactNode;
  description?: ReactNode;
  mark?: string | undefined;
};

/** Шапка панели: надзаголовок, имя, иконка вида, действия справа, описание
 * под именем во всю ширину. */
export function PanelHead({ eyebrow, name, mono = false, icon, actions, description, mark }: HeadProps): ReactElement {
  return (
    <header className="panel__head" data-testid={mark}>
      <span className="panel__head-icon">{icon}</span>
      <span className="panel__head-eyebrow">{eyebrow !== undefined && <Eyebrow>{eyebrow}</Eyebrow>}</span>
      <h3 className={mono ? "panel__name mono" : "panel__name"} data-testid="panel-name">
        {name}
      </h3>
      {actions !== undefined && <span className="panel__head-actions">{actions}</span>}
      {description !== undefined && description !== "" && (
        <p className="panel__description" data-testid="panel-description">
          {description}
        </p>
      )}
    </header>
  );
}

type SectionProps = {
  title?: ReactNode;
  actions?: ReactNode;
  /** Широкое содержимое (таблица) прокручивается внутри секции. */
  scroll?: boolean;
  mark?: string | undefined;
  children?: ReactNode;
};

/** Секция панели: заголовок капителью с действиями справа и содержимое. */
export function Section({ title, actions, scroll = false, mark, children }: SectionProps): ReactElement {
  const classes = ["section"];
  if (scroll) {
    classes.push("section--scroll");
  }

  return (
    <section className={classes.join(" ")} data-testid={mark}>
      {(title !== undefined || actions !== undefined) && <SectionHead actions={actions}>{title}</SectionHead>}
      {children}
    </section>
  );
}

export function SectionHead({ actions, children }: { actions?: ReactNode; children?: ReactNode }): ReactElement {
  return (
    <div className="section__head">
      {children !== undefined && <Eyebrow as="h4">{children}</Eyebrow>}
      {actions !== undefined && <span className="section__actions">{actions}</span>}
    </div>
  );
}

/** Абзац секции приглушённым: комментарий, описание. */
export function SectionText({ mark, children }: { mark?: string | undefined; children: ReactNode }): ReactElement {
  return (
    <p className="section__text" data-testid={mark}>
      {children}
    </p>
  );
}
