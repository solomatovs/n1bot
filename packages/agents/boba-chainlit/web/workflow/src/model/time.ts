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
