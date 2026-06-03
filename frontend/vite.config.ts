import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// strictPort: si 5173 está ocupado, falla en vez de saltar a 5174.
// El backend tiene CORS fijado a :5173, así que un puerto distinto rompería
// silenciosamente la app. Mejor un error ruidoso que un CORS roto.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
});
