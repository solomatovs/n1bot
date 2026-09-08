import { createContext, useContext } from "react";

import type { WorkflowApi } from "./api/client";
import type { RunSocket } from "./api/socket";
import type { HttpTransport } from "./api/transport";
import type { PageUrls } from "./config";

/** Общие для страниц службы: адреса, транспорт api, клиент над ним и сокет
 * ленты. Живут отдельно от App, чтобы страницы (и ленивый чанк каталога) не
 * тянули корень приложения статически.
 *
 * Профиль studio не выбирает: запросы идут без него, сервер берёт профиль
 * по умолчанию (general). Профили — механика chainlit; их роль здесь со
 * временем займут сами workflow. */
export type Services = {
  urls: PageUrls;
  transport: HttpTransport;
  api: WorkflowApi;
  socket: RunSocket;
};

export const ServicesContext = createContext<Services | null>(null);

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (services === null) {
    throw new Error("services are provided by App only");
  }

  return services;
}
