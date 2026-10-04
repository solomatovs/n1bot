import type { AnchorHTMLAttributes, ReactElement } from "react";

import "./Button.css";

import type { ButtonTone } from "./Button";

type Props = AnchorHTMLAttributes<HTMLAnchorElement> & {
  tone?: ButtonTone;
};

/** Ссылка в облике кнопки: внешние переходы (SSO), где нужен настоящий <a>. */
export function LinkButton({ tone = "default", className, children, ...rest }: Props): ReactElement {
  const classes = ["btn"];
  if (tone !== "default") {
    classes.push(`btn--${tone}`);
  }
  if (className !== undefined) {
    classes.push(className);
  }

  return (
    <a className={classes.join(" ")} {...rest}>
      {children}
    </a>
  );
}
