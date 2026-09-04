import { X } from "lucide-react";
import { useEffect, type ReactElement, type ReactNode } from "react";

import { IconButton } from "../../ui";

type Props = {
  title: string;
  /** Метка для тестов: data-dialog. */
  mark: string;
  onClose: () => void;
  children: ReactNode;
};

/** Модальное окно поверх сцены: форма потока, имя новой сущности, конфликт публикации.
 * Закрывается кнопкой и Escape; клик по подложке не закрывает, чтобы не терять ввод. */
export function Dialog({ title, mark, onClose, children }: Props): ReactElement {
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

  return (
    <div className="dialog-backdrop" data-dialog={mark}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title}>
        <header className="dialog__head">
          <h3 className="dialog__title">{title}</h3>
          <IconButton aria-label="close dialog" onClick={onClose}>
            <X size={16} />
          </IconButton>
        </header>
        <div className="dialog__body">{children}</div>
      </div>
    </div>
  );
}
