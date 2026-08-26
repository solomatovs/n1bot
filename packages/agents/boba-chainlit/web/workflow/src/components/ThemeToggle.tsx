import { Moon, Sun } from "lucide-react";
import { type ReactElement, useCallback, useState } from "react";

type Theme = "dark" | "light";

const STORAGE_KEY = "boba-workflow-theme";

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") {
      return stored;
    }
  } catch {
    // приватный режим: хранилища нет, тема по умолчанию
  }

  return "dark";
}

function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // хранилище недоступно — тема живёт до перезагрузки
  }
}

export function ThemeToggle(): ReactElement {
  const [theme, setTheme] = useState<Theme>(() => {
    const initial = readTheme();
    document.documentElement.setAttribute("data-theme", initial);
    return initial;
  });

  const toggle = useCallback(() => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  }, [theme]);

  return (
    <button type="button" className="icon-btn" onClick={toggle} title="Theme" aria-label="Theme">
      {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}
