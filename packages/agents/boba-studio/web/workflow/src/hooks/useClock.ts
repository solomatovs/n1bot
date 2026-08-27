import { useEffect, useState } from "react";

/** Текущее время раз в секунду, пока запуск идёт; после — замирает. */
export function useClock(live: boolean): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!live) {
      return;
    }

    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, 1000);

    return () => {
      window.clearInterval(timer);
    };
  }, [live]);

  return now;
}
