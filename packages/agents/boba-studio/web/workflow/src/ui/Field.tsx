import type { ReactElement, ReactNode } from "react";

import "./Field.css";

type Props = {
  /** Подпись поля; без неё рендерится только содержимое (ряды field--row). */
  label?: ReactNode | undefined;
  required?: boolean;
  /** Подпись моноширинным: имена аргументов. */
  mono?: boolean;
  hint?: string | undefined;
  /** Аргумент связан ребром: вместо ввода показывается источник. */
  bound?: string | undefined;
  issue?: string | undefined;
  invalid?: boolean;
  /** Ряд контролов в строку вместо колонки с подписью. */
  row?: boolean;
  /** Контрол перед подписью: чекбокс в строку. */
  controlFirst?: boolean;
  dataArg?: string | undefined;
  dataPath?: string | undefined;
  children?: ReactNode;
};

/** Поле формы: подпись, маркер обязательности, подсказка, замечание и сам
 * контрол. Единственное место, где существует класс `field`. */
export function Field({
  label,
  required = false,
  mono = false,
  hint,
  bound,
  issue,
  invalid = false,
  row = false,
  controlFirst = false,
  dataArg,
  dataPath,
  children,
}: Props): ReactElement {
  const classes = ["field"];
  if (row) {
    classes.push("field--row");
  }
  if (invalid) {
    classes.push("field--invalid");
  }

  const control =
    bound !== undefined && bound !== "" ? <span className="field__bound">◂ {bound}</span> : children;

  const body = (
    <>
      {controlFirst && control}
      {label !== undefined && (
        <span className={mono ? "field__label mono" : "field__label"}>
          {label}
          {required && <span className="field__required">*</span>}
        </span>
      )}
      {hint !== undefined && hint !== "" && <span className="field__hint">{hint}</span>}
      {!controlFirst && control}
      {issue !== undefined && issue !== "" && <span className="field__issue">{issue}</span>}
    </>
  );

  if (row && label === undefined) {
    return (
      <div className={classes.join(" ")} data-arg={dataArg} data-path={dataPath}>
        {body}
      </div>
    );
  }

  return (
    <label className={classes.join(" ")} data-arg={dataArg} data-path={dataPath}>
      {body}
    </label>
  );
}
