import { useEffect, useState } from "react";

/** Порог узкого экрана, общий со стилями (Layout.css, app.css): панели
 * становятся ящиками поверх сцены. */
const NARROW_MAX_WIDTH = 900;
const NARROW_QUERY = `(max-width: ${NARROW_MAX_WIDTH}px)`;

export function narrowScreen(): boolean {
  return window.matchMedia(NARROW_QUERY).matches;
}

/** Узкий ли экран сейчас: следит за сменой ширины окна. */
export function useNarrowScreen(): boolean {
  const [narrow, setNarrow] = useState(narrowScreen);

  useEffect(() => {
    const media = window.matchMedia(NARROW_QUERY);
    const onChange = (): void => {
      setNarrow(media.matches);
    };

    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);

  return narrow;
}
