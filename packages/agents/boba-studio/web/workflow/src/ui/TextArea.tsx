import type { ReactElement, TextareaHTMLAttributes } from "react";

import "./Input.css";

import { controlClasses, type ControlMods } from "./Input";

type Props = TextareaHTMLAttributes<HTMLTextAreaElement> & ControlMods;

/** Многострочный ввод: тексты, код, YAML. */
export function TextArea({ mono, code, className, ...rest }: Props): ReactElement {
  return <textarea className={controlClasses({ mono, code }, className)} {...rest} />;
}
