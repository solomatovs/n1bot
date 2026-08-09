import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ScrollText } from "lucide-react";

// Малозаметная иконка у шага инструмента: клик открывает живой вывод этого
// вызова в панели. После конца хода панель объяснит, что поток недоступен.
export default function CanvasStream() {
  const [busy, setBusy] = useState(false);

  const open = async () => {
    setBusy(true);
    try {
      await callAction({
        name: "canvas_stream",
        payload: { call_id: props.call_id },
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-6 w-6 opacity-50 hover:opacity-100"
      onClick={open}
      disabled={busy}
      title={`Живой вывод: ${props.label || props.call_id}`}
      aria-label="Показать вывод инструмента"
    >
      <ScrollText className="h-4 w-4" />
    </Button>
  );
}
