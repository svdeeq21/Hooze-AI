import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => { cleanup(); sessionStorage.clear(); });
window.confirm = () => true;                       // destructive buttons ask first; tests accept
window.HTMLElement.prototype.scrollIntoView = () => {};
Object.assign(navigator, { clipboard: { writeText: async () => {} } });
