<script setup>
/* 展板。
 *
 * 这一页要回答的是站在门口那一眼的问题：这个月挣了多少、哪几家店还没结上账、
 * 卡在哪。上一版把它做成了一张平铺的表，十几家店三个月就是几百个格子，
 * 什么都看得见等于什么都看不见。
 *
 * 所以顺序是：先四个数（全公司这个月），再一张所有店的明细表。逐月对比是同一批
 * 数字的另一种排法，收在标签页后面——两张表竖着摆的话，一屏之内看不完，人会以为
 * 下面那张是别的东西。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import { count, money, percent, signed, signedPct } from '../format'
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

//: 逐月对比里的账期，新的在上。矩阵那版横着摆六列，金额只能缩写成「15.2 万」——
//: 对账的人要的是 152,392.61，缩写只能看个大概，看个大概就得再点进去一次。
const shown = computed(() => app.periods)

/** 一行逐月对比：钱、利润率、和上个月差多少。 */
function line(period, list) {
  const done = list.filter((c) => c.revenue !== null && c.profit !== null)
  const revenue = done.reduce((a, c) => a + c.revenue, 0)
  const profit = done.reduce((a, c) => a + c.profit, 0)
  return {
    period,
    revenue: done.length ? revenue : null,
    profit: done.length ? profit : null,
    margin: revenue ? profit / revenue : null,
    stores: list.length,
    closed: list.filter((c) => c.state === 'closed').length,
    pending: list.length - done.length,
    cell: list.length === 1 ? list[0] : null,
  }
}

/** 上一行（更早那个月）到这一行差了多少。表格是新的在上，所以看的是下一行。 */
function withDelta(rows) {
  return rows.map((r, i) => {
    const before = rows[i + 1]
    const from = before?.profit
    const to = r.profit
    const can = typeof from === 'number' && typeof to === 'number'
    return {
      ...r,
      delta: can ? to - from : null,
      // 上个月是零或者亏的，百分比没有意义（除以零、或者「增长了 -300%」）。
      // 这种时候只给金额差。
      deltaPct: can && from > 0 ? (to - from) / from : null,
    }
  })
}

/** 全公司逐月。 */
const companyMonths = computed(() =>
  withDelta(shown.value.map((p) => line(p, cells.value.filter((c) => c.period === p)))),
)

/** 每家店逐月。 */
const storeMonths = computed(() => {
  const ids = [...new Set(cells.value.map((c) => c.store_id))]
  return ids.map((id) => {
    const mine = cells.value.filter((c) => c.store_id === id)
    const rows = withDelta(
      shown.value
        .filter((p) => mine.some((c) => c.period === p))
        .map((p) => line(p, mine.filter((c) => c.period === p))),
    )
    const done = mine.filter((c) => c.profit !== null)
    const revenue = done.reduce((a, c) => a + c.revenue, 0)
    const profit = done.reduce((a, c) => a + c.profit, 0)
    return {
      id,
      name: mine[0]?.store || id,
      platform: app.platforms.find((p) => p.id === mine[0]?.platform)?.name || mine[0]?.platform,
      rows,
      revenue,
      profit,
      margin: revenue ? profit / revenue : null,
    }
  }).sort((a, b) => b.profit - a.profit)
})

/** 各店逐月拉平成一张表：店名一行抬头，底下是这家店的每个月。 */
const monthRows = computed(() =>
  storeMonths.value.flatMap((s) => [
    { head: s },
    ...s.rows.map((r) => ({ ...r, store: s })),
  ]),
)

/** 所有账期加起来。 */
const span = computed(() => {
  const revenue = companyMonths.value.reduce((a, r) => a + (r.revenue || 0), 0)
  const profit = companyMonths.value.reduce((a, r) => a + (r.profit || 0), 0)
  return { revenue, profit, margin: revenue ? profit / revenue : null }
})

/** 涨了绿、跌了红。零和空不着色——不是好消息也不是坏消息。 */
function delta(v) {
  return { up: typeof v === 'number' && v > 0, neg: typeof v === 'number' && v < 0 }
}

//: 记住上次看的是哪个标签。切走再回来跳回默认，等于把人刚才翻到的地方扔了。
const tab = app.noted('board.tab', 'here')

/* 利润项逐月。
 *
 * 「这个月比上个月少了八万」这句话本身没有用，有用的是少在哪一项上。所以整张损益表
 * 按月摊开：收入、退款、推广、代发成本，一项一行，跟得到月，右边一列直接给出最近
 * 两个月的差额。合并多家店时逐项相加，凑不齐的格子会自己说明。 */

const trend = ref(null)
const trendBusy = ref(false)

async function pullTrend() {
  trendBusy.value = true
  try {
    trend.value = await api.trend({ store_id: app.storeId, platform: app.platform })
  } catch {
    trend.value = null
  } finally {
    trendBusy.value = false
  }
}

watch(
  [tab, () => app.storeId, () => app.platform, () => app.overview],
  () => {
    if (tab.value === 'months') pullTrend()
  },
  { immediate: true },
)

const trendPeriods = computed(() => trend.value?.periods || [])

/** 每一行补上「最近一个月比上个月」。 */
const trendRows = computed(() =>
  (trend.value?.rows || []).map((r) => {
    const [now, before] = trendPeriods.value
    const to = r.cells?.[now]?.value
    const from = r.cells?.[before]?.value
    const can = typeof to === 'number' && typeof from === 'number'
    return {
      ...r,
      delta: can ? to - from : null,
      // 费用项是负数，「多花了钱」是往下走。百分比按绝对值算，不然会出现
      // 「推广费涨了 -30%」这种要在脑子里绕一圈的说法。
      deltaPct: can && Math.abs(from) > 0 ? (Math.abs(to) - Math.abs(from)) / Math.abs(from) : null,
    }
  }),
)

/** 这一格是几家店加出来的。凑不齐要说，不能让人以为它是完整的。 */
function short(row, period) {
  const cell = row.cells?.[period]
  const all = trend.value?.stores?.[period] || 0
  if (!cell || cell.value === null || all <= 1) return ''
  return cell.stores < all ? `${all} 家店里 ${cell.stores} 家有这一项` : ''
}

function cellText(row, period) {
  const v = row.cells?.[period]?.value
  if (v === null || v === undefined) return '—'
  return row.display === 'percent' ? percent(v) : money(v)
}

function label(c) {
  if (!c) return ''
  if (c.state === 'closed') return c.stale ? '已结账 · 有新数据' : '已结账'
  if (c.blocking?.length) return `${c.blocking.length} 项拦住`
  if (c.missing?.length) return `缺 ${c.missing.length} 项`
  if (c.can_close) return '可结账'
  return '进行中'
}

/** 平台分组拉平成一张表。分组抬头留着，但不再各占一张卡。 */
const rows = computed(() =>
  groups.value.flatMap((g) => [{ head: g.name, size: g.list.length }, ...g.list]),
)

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

      <div class="card" style="margin-top: var(--s4)">
        <n-tabs v-model:value="tab" type="line" size="small">
          <n-tab-pane name="here" :tab="`本月各店（${here.length}）`">
            <div class="scroll tall">
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
                  <template v-for="r in rows">
                    <tr v-if="r.head" :key="`h-${r.head}`" class="quiet">
                      <td colspan="5" class="xs muted" style="padding-top: var(--s3)">
                        {{ r.head }} · {{ r.size }} 家店
                      </td>
                    </tr>
                    <tr v-else :key="r.store_id" style="cursor: pointer" @click="open(r)">
                      <td>{{ r.store }}</td>
                      <td class="right num">{{ money(r.revenue) }}</td>
                      <td class="right num" :class="{ neg: r.profit < 0 }">{{ money(r.profit) }}</td>
                      <td class="right num">
                        {{ r.revenue ? percent(r.profit / r.revenue) : '—' }}
                      </td>
                      <td>
                        <n-tag
                          size="small"
                          :type="r.state === 'closed' ? 'success' : r.blocking?.length ? 'error' : r.can_close ? 'info' : 'default'"
                          :bordered="false"
                        >
                          {{ label(r) }}
                        </n-tag>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </n-table>
            </div>
          </n-tab-pane>

          <n-tab-pane name="months" :tab="`逐月对比（${shown.length} 个账期）`">
            <div class="months">
              <div class="hd">
                <span class="strong">全公司逐月</span>
                <span class="xs muted">
                  金额是元，两位小数。「比上月」拿这个月的利润减上一个月的。
                </span>
              </div>
              <div class="scroll">
                <table>
                  <thead>
                    <tr>
                      <th>账期</th>
                      <th class="right">销售收入</th>
                      <th class="right">利润</th>
                      <th class="right">利润率</th>
                      <th class="right">比上月</th>
                      <th class="right">结账</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="r in companyMonths"
                      :key="r.period"
                      :class="{ now: r.period === period }"
                    >
                      <td class="num nowrap">
                        {{ r.period }}
                        <span v-if="r.period === period" class="tagline">本月</span>
                      </td>
                      <td class="right num big-num">{{ money(r.revenue) }}</td>
                      <td class="right num big-num" :class="{ neg: r.profit < 0 }">
                        {{ money(r.profit) }}
                      </td>
                      <td class="right num">{{ percent(r.margin) }}</td>
                      <td class="right num nowrap" :class="delta(r.delta)">
                        {{ signed(r.delta) }}
                        <span v-if="r.deltaPct !== null" class="pct">
                          {{ signedPct(r.deltaPct) }}
                        </span>
                      </td>
                      <td class="right num nowrap">
                        {{ r.closed }} / {{ r.stores }}
                        <span v-if="r.pending" class="warn xs">· {{ r.pending }} 家没数</span>
                      </td>
                    </tr>
                  </tbody>
                  <tfoot>
                    <tr>
                      <td class="num">合计</td>
                      <td class="right num big-num">{{ money(span.revenue) }}</td>
                      <td class="right num big-num" :class="{ neg: span.profit < 0 }">
                        {{ money(span.profit) }}
                      </td>
                      <td class="right num">{{ percent(span.margin) }}</td>
                      <td colspan="2" class="right xs muted">
                        {{ shown.length }} 个账期 · {{ count(cells.length) }} 个店期
                      </td>
                    </tr>
                  </tfoot>
                </table>
              </div>

              <div class="hd" style="margin-top: var(--s5)">
                <span class="strong">利润项逐月</span>
                <span class="xs muted">
                  {{ trend?.scope || '全公司' }} · 整张损益表按月摊开。
                  少了钱是少在哪一项上，这张表回答这个。
                </span>
              </div>
              <n-spin :show="trendBusy">
                <div v-if="trendRows.length" class="scroll tall">
                  <table class="items">
                    <thead>
                      <tr>
                        <th class="pin">利润项</th>
                        <th v-for="p in trendPeriods" :key="p" class="right">
                          {{ p }}
                          <span v-if="trend.stores?.[p] > 1" class="xs muted">
                            {{ trend.stores[p] }} 家店
                          </span>
                        </th>
                        <th v-if="trendPeriods.length > 1" class="right">
                          {{ trendPeriods[0] }} 比上月
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="r in trendRows"
                        :key="r.id"
                        :class="{ lv1: r.level === 1, lv2: r.level > 1, total: r.is_total }"
                      >
                        <td class="pin">{{ r.name }}</td>
                        <td
                          v-for="p in trendPeriods"
                          :key="p"
                          class="right num big-num"
                          :class="{ neg: r.cells?.[p]?.value < 0, na: r.cells?.[p]?.value == null }"
                          :title="short(r, p)"
                        >
                          {{ cellText(r, p) }}
                          <span v-if="short(r, p)" class="warn">*</span>
                        </td>
                        <td
                          v-if="trendPeriods.length > 1"
                          class="right num nowrap"
                          :class="delta(r.delta)"
                        >
                          <template v-if="r.display === 'percent'">—</template>
                          <template v-else>
                            {{ signed(r.delta) }}
                            <span v-if="r.deltaPct !== null" class="pct">
                              {{ signedPct(r.deltaPct) }}
                            </span>
                          </template>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p v-else class="small muted">这个范围里还没有算出数的账期。</p>
              </n-spin>
              <p v-if="trendRows.some((r) => trendPeriods.some((p) => short(r, p)))"
                 class="xs muted" style="margin-top: var(--s2)">
                带 <span class="warn">*</span> 的格子不是所有店都有这一项，鼠标停上去看是几家。
              </p>

              <div class="hd" style="margin-top: var(--s5)">
                <span class="strong">各店逐月</span>
                <span class="xs muted">点一行进那个月的账。</span>
              </div>
              <div class="scroll tall">
                <table>
                  <thead>
                    <tr>
                      <th>店铺 / 账期</th>
                      <th class="right">销售收入</th>
                      <th class="right">利润</th>
                      <th class="right">利润率</th>
                      <th class="right">比上月</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template v-for="r in monthRows">
                      <tr v-if="r.head" :key="`h-${r.head.id}`" class="group">
                        <td class="nowrap">
                          {{ r.head.name }}
                          <span class="xs muted">{{ r.head.platform }}</span>
                        </td>
                        <td class="right num">{{ money(r.head.revenue) }}</td>
                        <td class="right num" :class="{ neg: r.head.profit < 0 }">
                          {{ money(r.head.profit) }}
                        </td>
                        <td class="right num">{{ percent(r.head.margin) }}</td>
                        <td colspan="2" class="xs muted right">
                          {{ r.head.rows.length }} 个账期合计
                        </td>
                      </tr>
                      <tr
                        v-else
                        :key="`${r.store.id}-${r.period}`"
                        class="tap"
                        @click="open(r.cell)"
                      >
                        <td class="num nowrap indent">{{ r.period }}</td>
                        <td class="right num big-num">{{ money(r.revenue) }}</td>
                        <td class="right num big-num" :class="{ neg: r.profit < 0 }">
                          {{ money(r.profit) }}
                        </td>
                        <td class="right num">{{ percent(r.margin) }}</td>
                        <td class="right num nowrap" :class="delta(r.delta)">
                          {{ signed(r.delta) }}
                          <span v-if="r.deltaPct !== null" class="pct">
                            {{ signedPct(r.deltaPct) }}
                          </span>
                        </td>
                        <td>
                          <n-tag
                            size="small"
                            :type="r.cell?.state === 'closed' ? 'success' : r.cell?.blocking?.length ? 'error' : r.cell?.can_close ? 'info' : 'default'"
                            :bordered="false"
                          >
                            {{ label(r.cell) }}
                          </n-tag>
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
            </div>
          </n-tab-pane>
        </n-tabs>
      </div>
    </template>
  </n-spin>
</template>
