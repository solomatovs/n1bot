import type { LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, ReactElement } from "react";

import "./Button.css";

export type ButtonTone = "default" | "signal" | "primary" | "danger" | "ghost";
export type ButtonSize = "md" | "sm";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: ButtonTone;
  size?: ButtonSize;
  icon?: LucideIcon;
  /** На узком экране подпись прячется, остаётся иконка и title. */
  collapsible?: boolean;
};

/** Кнопка действия: тон и размер — типизированные варианты, иконка слева.
 * Высота из шкалы контролов (`--h-ctl`, `--h-ctl-sm`), чтобы ряд кнопок,
 * полей и переключателей выравнивался без подгонки.
 * Единственное место, где существует класс `btn`. */
export function Button({
  tone = "default",
  size = "md",
  icon: Icon,
  collapsible = false,
  className,
  children,
  type,
  title,
  ...rest
}: Props): ReactElement {
  const classes = ["btn"];
  if (tone !== "default") {
    classes.push(`btn--${tone}`);
  }
  if (size !== "md") {
    classes.push(`btn--${size}`);
  }
  if (collapsible) {
    classes.push("btn--collapsible");
  }
  if (className !== undefined) {
    classes.push(className);
  }

  let hint = title;
  if (collapsible && hint === undefined && typeof children === "string") {
    hint = children;
  }

  return (
    <button type={type ?? "button"} className={classes.join(" ")} title={hint} {...rest}>
      {Icon !== undefined && <Icon size={12} />}
      {children !== undefined && <span className="btn__label">{children}</span>}
    </button>
  );
}
