import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app";
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
