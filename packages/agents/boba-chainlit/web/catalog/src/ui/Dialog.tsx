import { X } from "lucide-react";
import { useEffect, type ReactElement, type ReactNode } from "react";

import "./Dialog.css";
import { IconButton } from "./IconButton";

type Props = {
  title: string;
  /** Метка для тестов: data-dialog. */
  mark: string;
  /** Широкое окно: редактор видов загрузки, шаринг. */
  wide?: boolean;
  onClose: () => void;
  children: ReactNode;
};

/** Модальное окно поверх сцены: форма потока, имя новой сущности, конфликт
 * публикации. Закрывается кнопкой и Escape; клик по подложке не закрывает,
 * чтобы не терять ввод. Единственное место с классами `dialog*`. */
export function Dialog({ title, mark, wide = false, onClose, children }: Props): ReactElement {
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const classes = ["dialog"];
  if (wide) {
    classes.push("dialog--wide");
  }

  return (
    <div className="dialog-backdrop" data-dialog={mark}>
      <div className={classes.join(" ")} role="dialog" aria-modal="true" aria-label={title}>
        <header className="dialog__head">
          <h3 className="dialog__title">{title}</h3>
          <IconButton aria-label="close dialog" size="sm" ghost onClick={onClose}>
            <X size={14} />
          </IconButton>
        </header>
        <div className="dialog__body">{children}</div>
      </div>
    </div>
  );
}
