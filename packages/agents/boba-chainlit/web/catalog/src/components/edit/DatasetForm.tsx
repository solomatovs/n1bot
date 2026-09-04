import { useState, type FormEvent, type ReactElement } from "react";

import type { Dataset, Layer } from "../../model/catalog";
import { Button, Field, Input, Select, TextArea } from "../../ui";

type Props = {
  dataset: Dataset;
  layers: Layer[];
  onSave: (dataset: Dataset) => void;
  onCancel: () => void;
};

/** Паспорт набора: имя, слой, источник, владелец, теги, описание. Сохранение отдаёт
 * набор целиком — операция set_dataset заменяет сущность, а не поля. */
export function DatasetForm({ dataset, layers, onSave, onCancel }: Props): ReactElement {
  const [name, setName] = useState(dataset.name);
  const [layerId, setLayerId] = useState(dataset.layer_id);
  const [source, setSource] = useState(dataset.source);
  const [owner, setOwner] = useState(dataset.owner);
  const [tags, setTags] = useState(dataset.tags.join(", "));
  const [description, setDescription] = useState(dataset.description);

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (name.trim() === "") {
      return;
    }

    onSave({
      ...dataset,
      name: name.trim(),
      layer_id: layerId,
      source: source.trim(),
      owner: owner.trim(),
      tags: tags
        .split(",")
        .map((tag) => tag.trim())
        .filter((tag) => tag !== ""),
      description: description.trim(),
    });
  };

  return (
    <form className="form" onSubmit={submit} data-testid="dataset-form">
      <Field label="name" required>
        <Input
          mono
          value={name}
          aria-label="dataset name"
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <Field label="layer">
        <Select
          value={layerId}
          aria-label="dataset layer"
          onChange={(event) => {
            setLayerId(event.target.value);
          }}
        >
          {layers.map((layer) => (
            <option key={layer.id} value={layer.id}>
              {layer.name}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="source">
        <Input
          mono
          value={source}
          aria-label="dataset source"
          onChange={(event) => {
            setSource(event.target.value);
          }}
        />
      </Field>
      <Field label="owner">
        <Input
          mono
          value={owner}
          aria-label="dataset owner"
          onChange={(event) => {
            setOwner(event.target.value);
          }}
        />
      </Field>
      <Field label="tags" hint="comma separated">
        <Input
          mono
          value={tags}
          aria-label="dataset tags"
          onChange={(event) => {
            setTags(event.target.value);
          }}
        />
      </Field>
      <Field label="description">
        <TextArea
          value={description}
          aria-label="dataset description"
          rows={3}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </Field>
      <div className="form__actions">
        <Button tone="primary" type="submit" disabled={name.trim() === ""}>
          save
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          cancel
        </Button>
      </div>
    </form>
  );
}
