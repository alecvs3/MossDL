import { resolve } from "node:path";
import { defineConfig, normalizePath } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const projectRoot = normalizePath(resolve("."));
const sourceRoot = `${projectRoot}/src`;
const watchedRootFiles = new Set([
  `${projectRoot}/index.html`,
]);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 1420,
    strictPort: true,
    watch: {
      // This repository also contains engines, captures, downloads, build
      // trees, and test artifacts. The UI dev server only needs live updates
      // from the application source and HTML entrypoint.
      ignored: (candidatePath) => {
        const candidate = normalizePath(resolve(candidatePath));
        return candidate !== projectRoot
          && candidate !== sourceRoot
          && !candidate.startsWith(`${sourceRoot}/`)
          && !watchedRootFiles.has(candidate);
      },
    },
  },

});
