import type { ReactElement, ReactNode } from "react";
import { Link } from "react-router-dom";

import "./IconButton.css";

type Props = {
  to: string;
  "aria-label": string;
  title?: string;
  children: ReactNode;
};

/** Иконка-ссылка топбара: та же геометрия, что IconButton, но переход роутером. */
export function IconLink({ to, title, children, ...rest }: Props): ReactElement {
  return (
    <Link to={to} className="icon-btn" title={title} {...rest}>
      {children}
    </Link>
  );
}
