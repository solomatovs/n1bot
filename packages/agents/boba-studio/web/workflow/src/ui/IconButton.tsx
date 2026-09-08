import type { ButtonHTMLAttributes, ReactElement } from "react";

import "./IconButton.css";

export type IconButtonSize = "sm" | "md" | "lg";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  "aria-label": string;
  size?: IconButtonSize;
  /** Без рамки и фона: кнопка в строке списка или заголовке секции. */
  ghost?: boolean;
};

/** Квадратная кнопка-иконка: размеры из шкалы контролов, sm — в строках
 * списков, lg — в шапке. Единственное место, где существует класс `icon-btn`. */
export function IconButton({ size = "md", ghost = false, className, children, type, ...rest }: Props): ReactElement {
  const classes = ["icon-btn"];
  if (size !== "md") {
    classes.push(`icon-btn--${size}`);
  }
  if (ghost) {
    classes.push("icon-btn--ghost");
  }
  if (className !== undefined) {
    classes.push(className);
  }

  return (
    <button type={type ?? "button"} className={classes.join(" ")} {...rest}>
      {children}
    </button>
  );
}
