import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 产物直接落进后端的 static 目录，由 FastAPI 挂载出去。分两个服务部署会多一个
// 要一起启停的东西，而这套系统是一个人维护的。
export default defineConfig({
  plugins: [vue()],
  base: '/static/',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: fileURLToPath(new URL('../ledger/static', import.meta.url)),
    emptyOutDir: true,
    // 财务盯着这套界面看一整天，加载快一点不如出错时看得懂——留着 sourcemap。
    sourcemap: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
