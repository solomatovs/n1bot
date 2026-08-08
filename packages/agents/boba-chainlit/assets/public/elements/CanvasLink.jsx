import { useState } from "react";
import { Button } from "@/components/ui/button";
import { PanelRight } from "lucide-react";

// Ссылка на файл в переписке: клик открывает панель справа. Живёт в истории
// треда, поэтому старый чат открывается со всеми своими диаграммами.
export default function CanvasLink() {
  const [busy, setBusy] = useState(false);

  const open = async () => {
    // открытая панель меняет содержимое сама: новый элемент с сервера
    // пересоздал бы её и снова проиграл анимацию открытия
    if (document.getElementById("side-view-content")) {
      window.dispatchEvent(
        new CustomEvent("boba:canvas", { detail: { path: props.path } })
      );
      return;
    }

    setBusy(true);
    try {
      await callAction({ name: "canvas_open", payload: { path: props.path } });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      variant="outline"
      className="w-fit max-w-full gap-2"
      onClick={open}
      disabled={busy}
      title={props.path}
    >
      <PanelRight />
      <span className="truncate">{props.label || props.path}</span>
    </Button>
  );
}
