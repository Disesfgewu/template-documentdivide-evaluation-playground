import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App initialTab={import.meta.env.VITE_LAB_DEV_TOKEN ? "workflow" : "operator"} />
  </StrictMode>,
);
