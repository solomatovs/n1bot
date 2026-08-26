import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app";
import "@fontsource/geist-sans/400.css";
import "@fontsource/geist-sans/500.css";
import "@fontsource/geist-sans/600.css";
import "@fontsource/geist-mono/400.css";
import "@fontsource/geist-mono/500.css";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/space-grotesk/700.css";
import "./styles/app.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("page has no #root");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
