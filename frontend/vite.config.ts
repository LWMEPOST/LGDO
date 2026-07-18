import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/console-static/",
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
  server: {
    proxy: {
      "/api": process.env.LGDO_API_PROXY || "http://127.0.0.1:8000",
    },
  },
});
