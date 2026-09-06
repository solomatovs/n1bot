import { type ReactElement } from "react";

import type { Layer } from "../../model/catalog";
import { Input, Select } from "../../ui";

/** Куда ставить узел: существующий слой либо новый, заводимый тут же по имени. */
export type LayerChoice = { kind: "existing"; id: string } | { kind: "new"; name: string };

type Props = {
  layers: Layer[];
  choice: LayerChoice;
  onChange: (choice: LayerChoice) => void;
  /** Подпись поля выбора для доступности и тестов. */
  label: string;
  fill?: boolean;
};

const NEW_LAYER = "__new__";

/** Первый выбор для списка слоёв: первый слой, а без слоёв — новый. */
export function defaultLayerChoice(layers: Layer[]): LayerChoice {
  const first = layers[0];
  if (first === undefined) {
    return { kind: "new", name: "" };
  }

  return { kind: "existing", id: first.id };
}

/** Выбор готов, когда указан слой или введено имя нового. */
export function layerChoiceReady(choice: LayerChoice): boolean {
  if (choice.kind === "existing") {
    return choice.id !== "";
  }

  return choice.name.trim() !== "";
}

/** Выбор слоя с пунктом «new layer…»: имя нового вводится рядом, узлы
 * без слоя не живут, поэтому слой заводится там же, где первый узел. */
export function LayerPicker({ layers, choice, onChange, label, fill }: Props): ReactElement {
  const value = choice.kind === "existing" ? choice.id : NEW_LAYER;

  return (
    <>
      <Select
        fill={fill}
        aria-label={label}
        value={value}
        onChange={(event) => {
          if (event.target.value === NEW_LAYER) {
            onChange({ kind: "new", name: "" });
            return;
          }

          onChange({ kind: "existing", id: event.target.value });
        }}
      >
        {layers.map((layer) => (
          <option key={layer.id} value={layer.id}>
            {layer.name}
          </option>
        ))}
        <option value={NEW_LAYER}>new layer…</option>
      </Select>
      {choice.kind === "new" && (
        <Input
          fill={fill}
          mono
          autoFocus
          aria-label="new layer name"
          placeholder="layer name"
          value={choice.name}
          onChange={(event) => {
            onChange({ kind: "new", name: event.target.value });
          }}
        />
      )}
    </>
  );
}
