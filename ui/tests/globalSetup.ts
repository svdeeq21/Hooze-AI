// Boots the REAL hooze-core API on seeded in-memory data (scripts/demo_server.py) so UI tests exercise real HTTP, not mocks.
import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";

const PORT = 8765;
let proc: ChildProcess | undefined;

export async function setup() {
  const root = path.resolve(__dirname, "../..");
  const py = process.env.HOOZE_PYTHON || "python3";
  proc = spawn(py, ["-m", "scripts.demo_server"], { cwd: root, env: { ...process.env, DEMO_PORT: String(PORT), DEMO_KEY: "test-key" }, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  proc.stdout?.on("data", (d) => (log += d)); proc.stderr?.on("data", (d) => (log += d));
  for (let i = 0; i < 80; i++) {
    try { if ((await fetch(`http://127.0.0.1:${PORT}/health`)).ok) { process.env.TEST_API = `http://127.0.0.1:${PORT}`; return; } } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("demo API did not start:\n" + log);
}
export async function teardown() { proc?.kill("SIGTERM"); }
