// Light / dark / follow-the-device. The choice is a per-browser convenience (localStorage); with none saved the device setting decides.
export type ThemeChoice = "system" | "light" | "dark";
const KEY = "hooze.theme";

export function getTheme(): ThemeChoice {
  try { const v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "system"; } catch { return "system"; }
}

export function applyTheme(t: ThemeChoice = getTheme()): void {
  const root = document.documentElement;
  if (t === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", t);
}

export function setTheme(t: ThemeChoice): void {
  try { if (t === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, t); } catch { /* private window: this tab only */ }
  applyTheme(t);
}
