import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Vite 开发配置，代理后端 FastAPI 数据接口。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
