import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './specs',
  timeout: 30_000,
  outputDir: '.artifacts',
  reporter: 'list',
  use: {
    baseURL: process.env.QUAGBOT_BASE_URL || 'http://127.0.0.1:3000',
    browserName: 'chromium',
    headless: process.env.PLAYWRIGHT_HEADLESS === 'true',
    screenshot: 'only-on-failure',
    storageState: process.env.QUAGBOT_STORAGE_STATE || '.auth/quagbot.json',
  },
});
