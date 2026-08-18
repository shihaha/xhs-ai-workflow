import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  use: { baseURL: "http://127.0.0.1:4173", trace: "retain-on-failure" },
  webServer: [
    {
      command: "python -m uvicorn frontend.e2e.fixture_app:app --host 127.0.0.1 --port 8000",
      cwd: "..",
      port: 8000,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173",
      cwd: ".",
      port: 4173,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
