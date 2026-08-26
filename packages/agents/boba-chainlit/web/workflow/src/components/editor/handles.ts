import type { PortRef } from "../../model/workflow";

/** Идентификаторы хэндлов узла редактора: сторона, вид порта, имя. */

export const HandleSide = {
  in: "in",
  out: "out",
} as const;

type Side = (typeof HandleSide)[keyof typeof HandleSide];

const SEP = ":";

export function handleId(side: Side, ref: Pick<PortRef, "kind" | "name">): string {
  if (ref.name === "") {
    return `${side}${SEP}${ref.kind}`;
  }

  return `${side}${SEP}${ref.kind}${SEP}${ref.name}`;
}

/** Порт по id хэндла и задаче узла; null — id чужой. */
export function portOfHandle(task: string, id: string | null | undefined): PortRef | null {
  if (id === null || id === undefined) {
    return null;
  }

  const [side, kind, ...rest] = id.split(SEP);
  if (side !== HandleSide.in && side !== HandleSide.out) {
    return null;
  }

  const name = rest.join(SEP);
  switch (kind) {
    case "task":
      return { task, kind: "task", name: "" };
    case "result":
      return { task, kind: "result", name: "" };
    case "arg":
      return name === "" ? null : { task, kind: "arg", name };
    case "fd":
      return name === "" ? null : { task, kind: "fd", name };
    default:
      return null;
  }
}
