import naive from 'naive-ui'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import './design.css'
import './app.css'
import { router } from './router'

// 部署了新版本，而这一页是旧版本打开的。
//
// 每个视图是单独一个分片，文件名带内容哈希。新版本一上去，旧文件名就没了，于是
// 开着旧页面的人点「总览」毫无反应——不报错、不跳转、什么都不发生，因为失败的是
// 一次动态 import。这种「点了没反应」最招人烦，而且看不出是版本问题。
window.addEventListener('vite:preloadError', () => window.location.reload())

// 整包注册。按需引入能省几百 KB，但这套系统跑在内网、用的人不超过十个，
// 省下的带宽换来的是每加一个组件就要回来改一次 import 清单。
createApp(App).use(naive).use(createPinia()).use(router).mount('#app')
