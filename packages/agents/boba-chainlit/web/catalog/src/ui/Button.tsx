import type { LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, ReactElement } from "react";

import "./Button.css";

export type ButtonTone = "default" | "signal" | "primary" | "danger" | "ghost";
export type ButtonSize = "md" | "tiny";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: ButtonTone;
  size?: ButtonSize;
  icon?: LucideIcon;
};

/** Кнопка действия: тон и размер — типизированные варианты, иконка слева.
 * Единственное место, где существует класс `btn`. */
export function Button({
  tone = "default",
  size = "md",
  icon: Icon,
  className,
  children,
  type,
  ...rest
}: Props): ReactElement {
  const classes = ["btn"];
  if (tone !== "default") {
    classes.push(`btn--${tone}`);
  }
  if (size !== "md") {
    classes.push(`btn--${size}`);
  }
  if (className !== undefined) {
    classes.push(className);
  }

  return (
    <button type={type ?? "button"} className={classes.join(" ")} {...rest}>
      {Icon !== undefined && <Icon size={12} />}
      {children}
    </button>
  );
}
