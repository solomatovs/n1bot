import type { ReactElement } from "react";

import type { ConnectionView } from "../../../model/account";
import type { SchemaDoc } from "../../../model/schema";
import { ConnectionForm } from "../../../components/connections/ConnectionForm";
import { Dialog } from "../../../ui";

type Props = {
  doc: SchemaDoc;
  /** Существующая строка — правка (общая — только чтение); null — новая. */
  row: ConnectionView | null;
  onSaved: (saved: ConnectionView) => void;
  onClose: () => void;
};

/** Общая форма соединения в модальном окне каталога. */
export function ConnectionDialog({ doc, row, onSaved, onClose }: Props): ReactElement {
  let title = "new connection";
  if (row !== null) {
    title = row.name;
  }

  return (
    <Dialog title={title} mark="connection-form" wide onClose={onClose}>
      <ConnectionForm doc={doc} row={row} onSaved={onSaved} onClose={onClose} />
    </Dialog>
  );
}
