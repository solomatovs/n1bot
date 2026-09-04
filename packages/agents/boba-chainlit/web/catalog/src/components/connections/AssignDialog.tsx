import { Link2 } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { ConnectionView, Source, SourceSpec } from "../../model/catalog";
import { Button, Dialog, Field, Form, Input, Note, Segmented, Select, Toolbar } from "../../ui";

type Mode = "existing" | "new";

type Props = {
  connection: ConnectionView;
  /** Источники того же вида, куда подключение можно поставить. */
  sources: Source[];
  onAssign: (sourceId: string) => void;
  onCreate: (spec: SourceSpec) => void;
  onClose: () => void;
};

const MODES: { value: Mode; label: string }[] = [
  { value: "existing", label: "existing source" },
  { value: "new", label: "new source" },
];

/** Пометить подключение источником: существующий того же вида либо новый —
 * именем и описанием; вид источника берётся у подключения. */
export function AssignDialog({ connection, sources, onAssign, onCreate, onClose }: Props): ReactElement {
  const [mode, setMode] = useState<Mode>(sources.length > 0 ? "existing" : "new");
  const [sourceId, setSourceId] = useState(sources[0]?.id ?? "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const trimmed = name.trim();
  const ready = mode === "existing" ? sourceId !== "" : trimmed !== "";

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (!ready) {
      return;
    }

    if (mode === "existing") {
      onAssign(sourceId);
      return;
    }

    onCreate({ name: trimmed, description: description.trim() });
  };

  return (
    <Dialog title={`source of ${connection.name}`} mark="assign-source" onClose={onClose}>
      <Form onSubmit={submit} mark="assign-form">
        <Note>
          A source groups connections of one kind under a name; this connection is{" "}
          <span className="mono">{connection.kind}</span>.
        </Note>
        <Segmented options={MODES} value={mode} onChange={setMode} label="assign mode" fill />
        {mode === "existing" && (
          <Field label="source" required hint={sources.length === 0 ? `no ${connection.kind} sources yet` : undefined}>
            <Select
              fill
              aria-label="assign to source"
              value={sourceId}
              disabled={sources.length === 0}
              onChange={(event) => {
                setSourceId(event.target.value);
              }}
            >
              {sources.map((source) => (
                <option key={source.id} value={source.id}>
                  {source.name}
                </option>
              ))}
            </Select>
          </Field>
        )}
        {mode === "new" && (
          <>
            <Field label="name" required>
              <Input
                fill
                mono
                autoFocus
                aria-label="new source name"
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                }}
              />
            </Field>
            <Field label="description">
              <Input
                fill
                aria-label="new source description"
                value={description}
                onChange={(event) => {
                  setDescription(event.target.value);
                }}
              />
            </Field>
          </>
        )}
        <Toolbar>
          <Button tone="primary" type="submit" icon={Link2} disabled={!ready} data-testid="assign-submit">
            {mode === "existing" ? "assign" : "create source"}
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
        </Toolbar>
      </Form>
    </Dialog>
  );
}
