import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: { environment: "jsdom", globals: true, globalSetup: ["./tests/globalSetup.ts"], setupFiles: ["./tests/setup.ts"], testTimeout: 20000, hookTimeout: 40000, fileParallelism: false },
});
