import { z } from "zod";

import { RunStatusSchema, TaskStatusSchema } from "./status";

/** Модели зеркалят pydantic-модели boba.workflow и хранилища; разбор — на границе API. */

export const PortRefKindSchema = z.enum(["task", "result", "arg", "fd"]);
export type PortRefKind = z.infer<typeof PortRefKindSchema>;

export const PortRefSchema = z.object({
  task: z.string(),
  kind: PortRefKindSchema,
  name: z.string(),
});
export type PortRef = z.infer<typeof PortRefSchema>;

export const EdgeKindSchema = z.enum(["stream", "value", "control"]);
export type EdgeKind = z.infer<typeof EdgeKindSchema>;

export const EdgeSchema = z.object({
  src: PortRefSchema,
  dst: PortRefSchema,
  kind: EdgeKindSchema,
});
export type Edge = z.infer<typeof EdgeSchema>;

export const PortDirectionSchema = z.enum(["read", "write"]);
export type PortDirection = z.infer<typeof PortDirectionSchema>;

export const TaskSpecSchema = z.object({
  tool: z.string(),
  args: z.record(z.unknown()),
  ports: z.record(PortDirectionSchema),
});
export type TaskSpec = z.infer<typeof TaskSpecSchema>;

export const WorkflowSpecSchema = z.object({
  name: z.string(),
  description: z.string(),
  tasks: z.record(TaskSpecSchema),
  edges: z.array(EdgeSchema),
});
export type WorkflowSpec = z.infer<typeof WorkflowSpecSchema>;

export const StageSchema = z.object({
  id: z.string(),
  tasks: z.array(z.string()),
  streams: z.array(EdgeSchema),
  after: z.array(z.string()),
});
export type Stage = z.infer<typeof StageSchema>;

export const ArgBindingSchema = z.object({
  arg: z.string(),
  sources: z.array(z.string()),
  template: z.string(),
});
export type ArgBinding = z.infer<typeof ArgBindingSchema>;

export const WorkflowGraphSchema = z.object({
  spec: WorkflowSpecSchema,
  stages: z.array(StageSchema),
  bindings: z.record(z.array(ArgBindingSchema)),
});
export type WorkflowGraph = z.infer<typeof WorkflowGraphSchema>;

export const TaskStateSchema = z.object({
  status: TaskStatusSchema,
  call_id: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  error: z.string(),
});
export type TaskState = z.infer<typeof TaskStateSchema>;

export const RunStateSchema = z.object({
  graph: WorkflowGraphSchema,
  status: RunStatusSchema,
  tasks: z.record(TaskStateSchema),
});
export type RunState = z.infer<typeof RunStateSchema>;

export const StoredWorkflowSchema = z.object({
  id: z.number().int(),
  user_id: z.number().int(),
  name: z.string(),
  spec: z.string(),
  tools: z.array(z.string()),
  layout: z.record(z.unknown()),
  created_at: z.string(),
  updated_at: z.string(),
});
export type StoredWorkflow = z.infer<typeof StoredWorkflowSchema>;

export const InitiatorSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("chat"), thread_id: z.string(), turn_id: z.string() }),
  z.object({ kind: z.literal("llm"), thread_id: z.string(), tool_call_id: z.string() }),
  z.object({ kind: z.literal("human"), via: z.enum(["page", "api"]) }),
  z.object({ kind: z.literal("schedule"), job_id: z.string(), job_run_id: z.string() }),
]);
export type Initiator = z.infer<typeof InitiatorSchema>;

export const StoredRunSchema = z.object({
  id: z.string().uuid(),
  workflow_id: z.number().int().nullable(),
  user_id: z.number().int(),
  initiator: InitiatorSchema,
  profile: z.string(),
  state: RunStateSchema,
  instance: z.string(),
  started_at: z.string(),
  finished_at: z.string().nullable(),
  status: RunStatusSchema,
});
export type StoredRun = z.infer<typeof StoredRunSchema>;

export const RunSnapshotSchema = z.object({
  run_id: z.string().uuid(),
  status: RunStatusSchema,
  state: RunStateSchema,
});
export type RunSnapshot = z.infer<typeof RunSnapshotSchema>;

export const ToolAvailabilitySchema = z.enum(["available", "denied", "chat_only"]);
export type ToolAvailability = z.infer<typeof ToolAvailabilitySchema>;

export const ToolArgSchema = z.object({ name: z.string(), required: z.boolean() });
export type ToolArg = z.infer<typeof ToolArgSchema>;

export const ToolPortSchema = z.object({ name: z.string(), direction: PortDirectionSchema });
export type ToolPort = z.infer<typeof ToolPortSchema>;

export const ToolFactsSchema = z.object({
  name: z.string(),
  availability: ToolAvailabilitySchema,
  args: z.array(ToolArgSchema),
  ports: z.array(ToolPortSchema),
  task_ports: z.boolean(),
});
export type ToolFacts = z.infer<typeof ToolFactsSchema>;

export const ToolCatalogSchema = z.record(ToolFactsSchema);
export type ToolCatalog = z.infer<typeof ToolCatalogSchema>;

export const RunStartedSchema = z.object({ run_id: z.string().uuid() });
export const StoppedSchema = z.object({ stopped: z.boolean() });
export const DeletedSchema = z.object({ deleted: z.boolean() });
