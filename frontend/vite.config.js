import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, Vite runs on :5173 and forwards API calls to Flask on :8000.
const api = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/process": api,
      "/transcribe": api,
      "/analyze": api,
      "/health": api,
    },
  },
});
