import type { ButtonHTMLAttributes, ReactElement } from "react";

import "./IconButton.css";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  "aria-label": string;
};

/** Квадратная кнопка-иконка топбара и панелей.
 * Единственное место, где существует класс `icon-btn`. */
export function IconButton({ className, children, type, ...rest }: Props): ReactElement {
  const classes = ["icon-btn"];
  if (className !== undefined) {
    classes.push(className);
  }

  return (
    <button type={type ?? "button"} className={classes.join(" ")} {...rest}>
      {children}
    </button>
  );
}
