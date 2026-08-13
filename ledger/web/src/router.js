import { createRouter, createWebHashHistory } from 'vue-router'

/* 用 hash 路由：这套服务是内网里一个 uvicorn 进程直接把静态文件挂出去的，
 * 没有反向代理帮忙把任意路径回落到 index.html。history 模式下人刷新一次
 * 就是 404，而对账时刷新页面是很自然的动作。
 */
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'board', component: () => import('./views/BoardView.vue') },
    { path: '/deliver', name: 'deliver', component: () => import('./views/DeliverView.vue') },
    {
      path: '/store/:id',
      name: 'period',
      component: () => import('./views/PeriodView.vue'),
      props: true,
    },
    {
      path: '/commission',
      name: 'commission',
      component: () => import('./views/CommissionView.vue'),
    },
    {
      path: '/onboard/:sha',
      name: 'onboard',
      component: () => import('./views/OnboardView.vue'),
      props: true,
    },
    { path: '/:rest(.*)', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
