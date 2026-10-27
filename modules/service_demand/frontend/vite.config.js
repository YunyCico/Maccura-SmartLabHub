import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // 相对路径：既能在根路径独立跑，也能挂在 /portal/ 下被平台内嵌
  base: './',
  server: {
    port: 5180,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true }
    }
  },
  build: { outDir: 'dist', emptyOutDir: true }
})