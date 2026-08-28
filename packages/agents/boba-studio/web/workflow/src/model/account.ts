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
  id: z.number(),
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
  models: z.array(z.string()),
  tools: z.array(z.string()),
});
export type ProfileView = z.infer<typeof ProfileViewSchema>;

export const ConnectionKindSchema = z.enum(["postgres", "clickhouse", "web"]);
export type ConnectionKind = z.infer<typeof ConnectionKindSchema>;

/** Профиль соединения — объект с дискриминатором kind; поля читает форма по виду. */
export const ConnectionProfileSchema = z.object({ kind: ConnectionKindSchema }).passthrough();
export type ConnectionProfile = z.infer<typeof ConnectionProfileSchema>;

export const ConnectionViewSchema = z.object({
  id: z.number(),
  name: z.string(),
  kind: ConnectionKindSchema,
  mine: z.boolean(),
  profile: ConnectionProfileSchema,
});
export type ConnectionView = z.infer<typeof ConnectionViewSchema>;

export type ConnectionBody = {
  name: string;
  profile: Record<string, unknown>;
};
