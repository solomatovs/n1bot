/** Выбранный профиль страницы: живёт в localStorage, пусто — профиль по умолчанию. */
const STORAGE_KEY = "boba-workflow-profile";

export function readProfile(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    // приватный режим: хранилища нет, профиль по умолчанию
    return "";
  }
}

export function writeProfile(name: string): void {
  try {
    if (name === "") {
      localStorage.removeItem(STORAGE_KEY);
      return;
    }

    localStorage.setItem(STORAGE_KEY, name);
  } catch {
    // хранилище недоступно — выбор живёт до перезагрузки
  }
}
