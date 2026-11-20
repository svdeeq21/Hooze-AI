import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App, makeQueryClient } from "./App";
import { applyTheme } from "./lib/theme";

applyTheme();       // before the first paint, so a light-theme user never sees a dark flash
createRoot(document.getElementById("root")!).render(<StrictMode><App client={makeQueryClient()} /></StrictMode>);
