import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "demand-radar-business-surface.spec.ts",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  use: { baseURL: "http://127.0.0.1:4174", trace: "retain-on-failure" },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4174",
    cwd: ".",
    port: 4174,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
