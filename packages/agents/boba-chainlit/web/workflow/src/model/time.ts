/** Время из API — ISO-строки; здесь единственное место их разбора и показа. */

export function parseInstant(iso: string | null): number | null {
  if (iso === null) {
    return null;
  }

  const millis = Date.parse(iso);
  if (Number.isNaN(millis)) {
    return null;
  }

  return millis;
}

export function formatInstant(iso: string | null): string {
  const millis = parseInstant(iso);
  if (millis === null) {
    return "—";
  }

  return new Date(millis).toLocaleString();
}

export function formatDuration(startIso: string | null, endIso: string | null): string {
  const start = parseInstant(startIso);
  if (start === null) {
    return "—";
  }

  const end = parseInstant(endIso) ?? Date.now();
  const millis = Math.max(0, end - start);
  if (millis < 1000) {
    return `${millis} ms`;
  }

  return `${(millis / 1000).toFixed(1)} s`;
}

export function formatAgo(iso: string | null, now: number = Date.now()): string {
  const millis = parseInstant(iso);
  if (millis === null) {
    return "—";
  }

  const seconds = Math.max(0, Math.round((now - millis) / 1000));
  if (seconds < 60) {
    return `${seconds} s ago`;
  }

  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes} min ago`;
  }

  const hours = Math.round(minutes / 60);
  if (hours < 48) {
    return `${hours} h ago`;
  }

  return `${Math.round(hours / 24)} d ago`;
}

/** Отметка оси таймлайна: m:ss от старта запуска. */
export function formatClock(millis: number): string {
  const total = Math.max(0, Math.round(millis / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export function formatMs(millis: number): string {
  if (millis < 1000) {
    return `${millis} ms`;
  }

  const total = Math.round(millis / 1000);
  if (total < 60) {
    return `${total}s`;
  }

  return `${Math.floor(total / 60)}m ${total % 60}s`;
}
