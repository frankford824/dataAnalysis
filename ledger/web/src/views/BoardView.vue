<script setup>
/* 展板。
 *
 * 这一页要回答的是站在门口那一眼的问题：这个月挣了多少、哪几家店还没结上账、
 * 卡在哪。上一版把它做成了一张平铺的表，十几家店三个月就是几百个格子，
 * 什么都看得见等于什么都看不见。
 *
 * 所以顺序是：先四个数（全公司这个月），再按平台分组的店铺卡，最后才是完整矩阵。
 * 越往下越细，人可以在任何一层停下。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { brief, count, money, percent } from '../format'
import { useApp } from '../store'
import DropZone from '../components/DropZone.vue'

const app = useApp()
const router = useRouter()

const period = computed(() => app.period || app.periods[0] || '')

const cells = computed(() =>
  (app.overview?.cells || []).filter(
    (c) => !app.platform || c.platform === app.platform,
  ),
)

const here = computed(() => cells.value.filter((c) => c.period === period.value))

const totals = computed(() => {
  const rows = here.value.filter((c) => c.revenue !== null && c.profit !== null)
  const revenue = rows.reduce((a, c) => a + c.revenue, 0)
  const profit = rows.reduce((a, c) => a + c.profit, 0)
  return {
    revenue,
    profit,
    margin: revenue ? profit / revenue : null,
    closed: here.value.filter((c) => c.state === 'closed').length,
    stuck: here.value.filter((c) => c.state !== 'closed' && !c.can_close).length,
    incomplete: here.value.length - rows.length,
  }
})

/** 按平台分组，组内按利润从低到高——要人管的都在上面。 */
const groups = computed(() => {
  const by = new Map()
  for (const c of here.value) {
    const key = c.platform || '(未分平台)'
    if (!by.has(key)) by.set(key, [])
    by.get(key).push(c)
  }
  for (const list of by.values()) {
    list.sort((a, b) => (a.profit ?? 0) - (b.profit ?? 0))
  }
  return [...by].map(([platform, list]) => ({
    platform,
    name: app.platforms.find((p) => p.id === platform)?.name || platform,
    list,
  }))
})

const shown = computed(() => app.periods.slice(0, 6))

const storeRows = computed(() => {
  const ids = [...new Set(cells.value.map((c) => c.store_id))]
  return ids.map((id) => ({
    id,
    name: cells.value.find((c) => c.store_id === id)?.store || id,
    byPeriod: Object.fromEntries(
      cells.value.filter((c) => c.store_id === id).map((c) => [c.period, c]),
    ),
  }))
})

function label(c) {
  if (!c) return ''
  if (c.state === 'closed') return c.stale ? '已结账 · 有新数据' : '已结账'
  if (c.blocking?.length) return `${c.blocking.length} 项拦住`
  if (c.missing?.length) return `缺 ${c.missing.length} 项`
  if (c.can_close) return '可结账'
  return '进行中'
}

function open(c) {
  if (!c) return
  app.pick({ store: c.store_id, period: c.period })
  router.push({ name: 'period', params: { id: c.store_id }, query: { period: c.period } })
}
</script>

<template>
  <n-spin :show="app.loading">
    <div v-if="!app.overview?.cells?.length" class="card">
      <n-empty description="还没有账">
        <template #extra>
          <div class="small muted" style="max-width: 420px; margin-bottom: var(--s4)">
            把一个月的表拖进来就行——订单明细、对账、运费、推广，有几张交几张。
            店铺和账期从文件名认，不用先建店。
          </div>
          <DropZone />
        </template>
      </n-empty>
    </div>

    <template v-else>
      <div class="board-kpis">
        <div class="kpi">
          <div class="label">销售收入</div>
          <div class="value">{{ money(totals.revenue) }}</div>
          <div class="foot">{{ period }} · {{ here.length }} 家店</div>
        </div>
        <div class="kpi">
          <div class="label">利润</div>
          <div class="value" :class="{ neg: totals.profit < 0 }">{{ money(totals.profit) }}</div>
          <div class="foot">利润率 {{ percent(totals.margin) }}</div>
        </div>
        <div class="kpi">
          <div class="label">已结账</div>
          <div class="value">{{ totals.closed }} / {{ here.length }}</div>
          <div class="foot">{{ totals.incomplete ? `${totals.incomplete} 家还没算出数` : '都算出数了' }}</div>
        </div>
        <div class="kpi">
          <div class="label">结不了</div>
          <div class="value" :class="{ neg: totals.stuck > 0 }">{{ totals.stuck }}</div>
          <div class="foot">{{ totals.stuck ? '点开看卡在哪' : '没有卡住的' }}</div>
        </div>
      </div>

      <div v-for="g in groups" :key="g.platform" class="card" style="margin-top: var(--s4)">
        <header>
          <h2>{{ g.name }}</h2>
          <span class="sub">{{ g.list.length }} 家店 · {{ period }}</span>
        </header>
        <n-table size="small" :bordered="false" :single-line="false">
          <thead>
            <tr>
              <th>店铺</th>
              <th class="right">销售收入</th>
              <th class="right">利润</th>
              <th class="right">利润率</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in g.list" :key="c.store_id" style="cursor: pointer" @click="open(c)">
              <td>{{ c.store }}</td>
              <td class="right num">{{ money(c.revenue) }}</td>
              <td class="right num" :class="{ neg: c.profit < 0 }">{{ money(c.profit) }}</td>
              <td class="right num">
                {{ c.revenue ? percent(c.profit / c.revenue) : '—' }}
              </td>
              <td>
                <n-tag
                  size="small"
                  :type="c.state === 'closed' ? 'success' : c.blocking?.length ? 'error' : c.can_close ? 'info' : 'default'"
                  :bordered="false"
                >
                  {{ label(c) }}
                </n-tag>
              </td>
            </tr>
          </tbody>
        </n-table>
      </div>

      <div class="card" style="margin-top: var(--s4)">
        <header>
          <h2>每家店每个月</h2>
          <span class="sub">最近 {{ shown.length }} 个账期，点格子进去</span>
        </header>
        <div class="matrix">
          <table>
            <thead>
              <tr>
                <th />
                <th v-for="p in shown" :key="p" class="num">{{ p }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in storeRows" :key="row.id">
                <th style="white-space: nowrap">{{ row.name }}</th>
                <td v-for="p in shown" :key="p">
                  <button
                    v-if="row.byPeriod[p]"
                    class="cell"
                    :class="{
                      closed: row.byPeriod[p].state === 'closed',
                      blocked: row.byPeriod[p].blocking?.length,
                      stale: row.byPeriod[p].stale,
                    }"
                    @click="open(row.byPeriod[p])"
                  >
                    <div class="amt" :class="{ neg: row.byPeriod[p].profit < 0 }">
                      {{ brief(row.byPeriod[p].profit) }}
                    </div>
                    <div class="state">{{ label(row.byPeriod[p]) }}</div>
                  </button>
                  <div v-else class="cell empty-cell">
                    <div class="amt">—</div>
                    <div class="state">没交表</div>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="xs muted" style="margin-top: var(--s3)">
          格子里是利润。{{ count(cells.length) }} 个店期。
        </p>
      </div>

      <DropZone style="margin-top: var(--s4)" />
    </template>
  </n-spin>
</template>
