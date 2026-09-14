import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [
    react(),
    {
      name: "site-scope-canonical-local-origin",
      configureServer(server) {
        server.middlewares.use((request, response, next) => {
          const requestInfo = request as unknown as {
            headers: { host?: string };
            url?: string;
          };
          const host = String(requestInfo.headers.host || "").toLowerCase();
          if (host === "localhost" || host === "localhost:80") {
            next();
            return;
          }
          response.statusCode = 307;
          response.setHeader(
            "Location",
            `http://localhost${requestInfo.url || "/"}`,
          );
          response.end();
        });
      },
    },
  ],
  server: {
    host: "localhost",
    port: 80,
    strictPort: true,
  },
});
