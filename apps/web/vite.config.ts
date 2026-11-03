import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const engine = process.env.BENCHMATE_ENGINE_URL ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/realtime': engine,
      '/health': engine,
      '/ws': { target: engine.replace(/^http/, 'ws'), ws: true },
    },
  },
});
