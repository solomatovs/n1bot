import { z } from "zod";

/** Модели входа, профиля и соединений: зеркала pydantic-ответов boba.studio.api. */

export const SignInProvidersSchema = z.object({
  password: z.boolean(),
  sso_url: z.string(),
});
export type SignInProviders = z.infer<typeof SignInProvidersSchema>;

export const SignInSchema = z.object({
  provider: z.string(),
  principal: z.string(),
  ticket: z.boolean(),
});

export const MeSchema = z.object({
  id: z.string().uuid(),
  login: z.string(),
  roles: z.array(z.string()),
  profile: z.string(),
  sign_in: SignInSchema,
});
export type Me = z.infer<typeof MeSchema>;

export const ProfileViewSchema = z.object({
  name: z.string(),
  display_name: z.string(),
  description: z.string(),
  icon: z.string(),
  default: z.boolean(),
  tools: z.array(z.string()),
});
export type ProfileView = z.infer<typeof ProfileViewSchema>;

/** Виды соединений приносят плагины сервера: перечня на фронте нет, kind — строка. */
export const ConnectionKindSchema = z.string();
export type ConnectionKind = z.infer<typeof ConnectionKindSchema>;

/** Соединение — объект с дискриминатором kind; поля читает форма по виду. */
export const ConnectionSchema = z.object({ kind: ConnectionKindSchema }).passthrough();
export type Connection = z.infer<typeof ConnectionSchema>;

export const ConnectionViewSchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  kind: ConnectionKindSchema,
  mine: z.boolean(),
  /** false — тип строки не установлен: соединения нет, вместо формы — пометка. */
  available: z.boolean(),
  connection: ConnectionSchema.nullable(),
});
export type ConnectionView = z.infer<typeof ConnectionViewSchema>;

export type ConnectionBody = {
  name: string;
  connection: Record<string, unknown>;
};

export const ProbeResultSchema = z.object({
  ok: z.boolean(),
  message: z.string(),
  elapsed_ms: z.number(),
});
export type ProbeResult = z.infer<typeof ProbeResultSchema>;
