import { Button } from "@/components/ui/button";
import { ExternalLink } from "lucide-react";

// Ссылка на страницу каталога в переписке: черновик или вид открывается
// отдельной вкладкой. Живёт в истории треда, поэтому старый чат открывается
// со всеми своими ссылками.
export default function CatalogLink() {
  const open = () => {
    if (typeof window === "undefined") return;
    window.open(props.url, "_blank", "noopener");
  };

  return (
    <Button
      variant="outline"
      className="w-fit max-w-full gap-2"
      onClick={open}
      title={props.url}
    >
      <ExternalLink />
      <span className="truncate">
        {props.kind}: {props.label || props.url}
      </span>
    </Button>
  );
}
