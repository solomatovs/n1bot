import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";

import "./Toast.css";

export type ToastTone = "info" | "success" | "error";

/** Показ всплывашки: текст и тон; повторный вызов добавляет новую карточку. */
export type ToastFn = (text: string, tone?: ToastTone) => void;

type ToastItem = {
  id: number;
  text: string;
  tone: ToastTone;
  leaving: boolean;
};

const ToastContext = createContext<ToastFn | null>(null);

/** Время жизни всплывашки и хвост анимации ухода. */
const TOAST_LIFE_MS = 4000;
const TOAST_FADE_MS = 300;

export function useToast(): ToastFn {
  const toast = useContext(ToastContext);
  if (toast === null) {
    throw new Error("toasts are provided by ToastProvider only");
  }

  return toast;
}

/** Центральные уведомления страницы: фиксированный оверлей поверх сцены,
 * карточки не двигают остальной UI и сами уходят через несколько секунд.
 *
 * Провайдер живёт в App поверх роутера; любой компонент показывает
 * сообщение через useToast(). */
export function ToastProvider({ children }: { children: ReactNode }): ReactElement {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, leaving: true } : item)));
    window.setTimeout(() => {
      setItems((current) => current.filter((item) => item.id !== id));
    }, TOAST_FADE_MS);
  }, []);

  const toast = useCallback<ToastFn>(
    (text, tone = "info") => {
      const id = nextId.current;
      nextId.current += 1;

      setItems((current) => [...current, { id, text, tone, leaving: false }]);
      window.setTimeout(() => {
        dismiss(id);
      }, TOAST_LIFE_MS);
    },
    [dismiss],
  );

  const shown = useMemo(() => items, [items]);

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {shown.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`toast toast--${item.tone}${item.leaving ? " toast--leaving" : ""}`}
            data-tone={item.tone}
            onClick={() => {
              dismiss(item.id);
            }}
          >
            {item.text}
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
