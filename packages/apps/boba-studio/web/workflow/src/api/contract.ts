import type { components } from "./schema";
import type { ConnectionView, Me, ProfileView, SignInProviders } from "../model/account";

/** Сверка zod-моделей страницы с OpenAPI-схемой API на этапе компиляции:
 * разбор на границе остаётся у zod, а расхождение полей ломает сборку.
 * Модели workflow не сверяются: его REST отключён и в схеме их нет. */
export type Schemas = components["schemas"];
export type Extends<A, B> = [A] extends [B] ? true : false;
export type Assert<T extends true> = T;

export type Contract = [
  Assert<Extends<Me, Schemas["Me"]>>,
  Assert<Extends<ProfileView, Schemas["ProfileView"]>>,
  Assert<Extends<SignInProviders, Schemas["SignInProviders"]>>,
  Assert<Extends<Omit<ConnectionView, "connection">, Omit<Schemas["ConnectionView"], "connection">>>,
];
