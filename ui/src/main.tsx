import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App, makeQueryClient } from "./App";

createRoot(document.getElementById("root")!).render(<StrictMode><App client={makeQueryClient()} /></StrictMode>);
