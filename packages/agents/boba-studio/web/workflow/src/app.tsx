import { createContext, type ReactElement, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";

import { WorkflowApi } from "./api/client";
import { RunSocket } from "./api/socket";
import { Shell } from "./components/shell/Shell";
import { PageUrls, pageConfig } from "./config";
import { readProfile, writeProfile } from "./model/profile";
import { AccountPage } from "./pages/AccountPage";
import { BuildPage } from "./pages/BuildPage";
import { LoginPage } from "./pages/LoginPage";
import { ObservePage } from "./pages/ObservePage";

/** Общие для страниц службы: адреса и API-клиент. */
export type Services = {
  urls: PageUrls;
  api: WorkflowApi;
  socket: RunSocket;
  /** Смена профиля: новый api-клиент, и всё, что зависит от api, перечитывается. */
  chooseProfile: (name: string) => void;
};

const ServicesContext = createContext<Services | null>(null);

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (services === null) {
    throw new Error("services are provided by App only");
  }

  return services;
}

function LegacyRun(): ReactElement {
  const { runId } = useParams();
  return <Navigate to={`/observe/${runId ?? ""}`} replace />;
}

function LegacyWorkflow(): ReactElement {
  const { workflowId } = useParams();
  return <Navigate to={`/build/${workflowId ?? ""}`} replace />;
}

/** 401 от api в любом месте уводит на вход, запоминая, откуда ушли. */
function SignedInOnly(): ReactElement {
  const { api } = useServices();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    api.onUnauthorized(() => {
      void navigate("/login", { replace: true, state: { next: `${location.pathname}${location.search}` } });
    });

    return () => {
      api.onUnauthorized(null);
    };
  }, [api, navigate, location.pathname, location.search]);

  return <Outlet />;
}

export function App(): ReactElement {
  const urls = useMemo(() => new PageUrls(pageConfig()), []);
  const socket = useRef<RunSocket | null>(null);
  socket.current ??= new RunSocket(urls);
  const [profile, setProfile] = useState(() => readProfile());
  const chooseProfile = useCallback((name: string) => {
    writeProfile(name);
    setProfile(name);
  }, []);
  const liveSocket = socket.current;
  const services = useMemo<Services>(
    () => ({ urls, api: new WorkflowApi(urls, profile), socket: liveSocket, chooseProfile }),
    [urls, profile, liveSocket, chooseProfile],
  );

  return (
    <ServicesContext.Provider value={services}>
      <BrowserRouter basename={services.urls.routerBase}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<SignedInOnly />}>
            <Route path="/account" element={<AccountPage />} />
            <Route element={<Shell mode="observe" />}>
              <Route path="/observe" element={<ObservePage />} />
              <Route path="/observe/:runId" element={<ObservePage />} />
            </Route>
            <Route element={<Shell mode="build" />}>
              <Route path="/build" element={<BuildPage />} />
              <Route path="/build/new" element={<BuildPage />} />
              <Route path="/build/:workflowId" element={<BuildPage />} />
            </Route>
          </Route>
          <Route path="/run/:runId" element={<LegacyRun />} />
          <Route path="/w/:workflowId" element={<LegacyWorkflow />} />
          <Route path="/new" element={<Navigate to="/build/new" replace />} />
          <Route path="*" element={<Navigate to="/observe" replace />} />
        </Routes>
      </BrowserRouter>
    </ServicesContext.Provider>
  );
}
