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

//: 逐月对比里的账期，新的在上。
//:
//: 这一块返工过两次，两次都错在同一个地方。第一版横着摆六列矩阵，金额只能缩写成
//: 「15.2 万」——对账的人要的是 152,392.61。第二版把金额写全了，代价是三张表竖着
//: 摞起来，页面拉得比原来还长，等于用「翻不完」换了「看不清」。
//:
//: 现在只留一张表：一个账期一行，一屏看完。细节全在点开之后的弹框里——利润项、
//: 各店、和上个月差多少。要看细节的人本来就要点一下，不看细节的人不该被迫滚过去。
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

/* 一个月的细账。
 *
 * 「这个月比上个月少了八万」这句话本身没有用，有用的是少在哪一项上。所以点开一个
 * 账期，弹框里给两张表：整张损益表这个月和上个月各是多少、差多少；以及这个月各家店
 * 分别是多少。两张表都写全额，因为它们已经不占主页面的位置了。 */

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

/* 点开的是哪个月。
 *
 * 开关和账期是两个值，不能合成一个。合成一个的话，关弹框就得把账期清掉，而弹框
 * 退场还有一段动画——那段时间里标题会变成「 · 全公司」，看着像是弹出了第二个空框。 */
const detail = ref('')
const detailOpen = ref(false)

function openMonth(period) {
  detail.value = period
  detailOpen.value = true
  if (!trend.value && !trendBusy.value) pullTrend()
}

/** 弹框顶上那一行数：这个月的收入、利润、利润率、比上月。 */
const detailHead = computed(() =>
  companyMonths.value.find((r) => r.period === detail.value) || null,
)

//: 上一个月。差额都是跟它比出来的。
const detailPrev = computed(() => {
  const i = shown.value.indexOf(detail.value)
  return i >= 0 ? shown.value[i + 1] || '' : ''
})

/** 损益表逐项：这个月、上个月、差多少。 */
const detailItems = computed(() => {
  const now = detail.value
  const before = detailPrev.value
  if (!now) return []
  const all = trend.value?.stores?.[now] || 0
  return (trend.value?.rows || [])
    .map((r) => {
      const to = r.cells?.[now]?.value ?? null
      const from = before ? r.cells?.[before]?.value ?? null : null
      const can = typeof to === 'number' && typeof from === 'number'
      const stores = r.cells?.[now]?.stores ?? 0
      return {
        ...r,
        now: to,
        before: from,
        delta: can ? to - from : null,
        // 费用项是负数，「多花了钱」是往下走。百分比按绝对值算，不然会出现
        // 「推广费涨了 -30%」这种要在脑子里绕一圈的说法。比率行不算百分比，
        // 它的差额本来就是百分点。
        deltaPct:
          can && r.display !== 'percent' && Math.abs(from) > 0
            ? (Math.abs(to) - Math.abs(from)) / Math.abs(from)
            : null,
        // 这一项不是所有店都有。不说的话，人会把它当成完整的合计。
        partial: to !== null && all > 1 && stores < all ? `${all} 家店里 ${stores} 家有这一项` : '',
      }
    })
    .filter((r) => r.now !== null || r.before !== null)
})

/** 这个月各家店，利润高的在上。 */
const detailStores = computed(() => {
  const now = detail.value
  const before = detailPrev.value
  if (!now) return []
  return cells.value
    .filter((c) => c.period === now)
    .map((c) => {
      const was = before
        ? cells.value.find((x) => x.store_id === c.store_id && x.period === before)
        : null
      const can = typeof c.profit === 'number' && typeof was?.profit === 'number'
      return {
        ...c,
        delta: can ? c.profit - was.profit : null,
        deltaPct: can && was.profit > 0 ? (c.profit - was.profit) / was.profit : null,
      }
    })
    .sort((a, b) => (b.profit ?? -Infinity) - (a.profit ?? -Infinity))
})

/** 比率行的差额单位是百分点，跟金额行不是一回事，写出来免得被当成涨跌幅。 */
function deltaText(r) {
  if (r.delta === null) return '—'
  return r.display === 'percent' ? `${signedPct(r.delta)} 个点` : signed(r.delta)
}

function amount(v, display) {
  if (v === null || v === undefined) return '—'
  return display === 'percent' ? percent(v) : money(v)
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
  // 从弹框里点进去要先关掉它。留着的话，浏览器一按返回，页面底下先冒出一个
  // 上次开着的弹框，人得先关掉它才看得见自己回到了哪。
  detailOpen.value = false
  app.pick({ store: c.store_id, period: c.period })
  router.push({ name: 'period', params: { id: c.store_id }, query: { period: c.period } })
}

//: 换店、换平台之后，弹框里那个月的数已经不是刚才看的那批了，关掉重来。
watch([() => app.storeId, () => app.platform], () => {
  detailOpen.value = false
})
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
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="r in companyMonths"
                      :key="r.period"
                      class="tap"
                      :class="{ now: r.period === period }"
                      @click="openMonth(r.period)"
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
                      <td class="right xs go">看明细</td>
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
                      <td colspan="3" class="right xs muted">
                        {{ shown.length }} 个账期 · {{ count(cells.length) }} 个店期 ·
                        点一行看这个月的利润项和各店明细
                      </td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </div>
          </n-tab-pane>
        </n-tabs>
      </div>
    </template>

    <!-- 一个月的细账。主页面只放一行一个月，细节都收在这里。 -->
    <n-modal
      v-model:show="detailOpen"
      preset="card"
      class="month-detail"
      :style="{ maxWidth: '960px' }"
      :title="`${detail} · ${trend?.scope || '全公司'}`"
    >
      <template #header-extra>
        <span class="xs muted">
          {{ detailPrev ? `跟 ${detailPrev} 比` : '没有上一个账期可比' }}
        </span>
      </template>

      <div v-if="detailHead" class="sum">
        <div>
          <div class="label">销售收入</div>
          <div class="value">{{ money(detailHead.revenue) }}</div>
        </div>
        <div>
          <div class="label">利润</div>
          <div class="value" :class="{ neg: detailHead.profit < 0 }">
            {{ money(detailHead.profit) }}
          </div>
        </div>
        <div>
          <div class="label">利润率</div>
          <div class="value">{{ percent(detailHead.margin) }}</div>
        </div>
        <div>
          <div class="label">比上月</div>
          <div class="value" :class="delta(detailHead.delta)">
            {{ signed(detailHead.delta) }}
            <span v-if="detailHead.deltaPct !== null" class="pct">
              {{ signedPct(detailHead.deltaPct) }}
            </span>
          </div>
        </div>
      </div>

      <n-tabs type="segment" size="small" default-value="items" style="margin-top: var(--s4)">
        <n-tab-pane name="items" tab="利润项">
          <n-spin :show="trendBusy">
            <div v-if="detailItems.length" class="scroll tall">
              <table class="items">
                <thead>
                  <tr>
                    <th class="pin">项目</th>
                    <th class="right">{{ detail }}</th>
                    <th v-if="detailPrev" class="right">{{ detailPrev }}</th>
                    <th v-if="detailPrev" class="right">差额</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="r in detailItems"
                    :key="r.id"
                    :class="{ lv1: r.level === 1, lv2: r.level > 1, total: r.is_total }"
                  >
                    <td class="pin">
                      {{ r.name }}
                      <span v-if="r.partial" class="warn" :title="r.partial">*</span>
                    </td>
                    <td class="right num big-num" :class="{ neg: r.now < 0, na: r.now === null }">
                      {{ amount(r.now, r.display) }}
                    </td>
                    <td
                      v-if="detailPrev"
                      class="right num muted"
                      :class="{ na: r.before === null }"
                    >
                      {{ amount(r.before, r.display) }}
                    </td>
                    <td v-if="detailPrev" class="right num nowrap" :class="delta(r.delta)">
                      {{ deltaText(r) }}
                      <span v-if="r.deltaPct !== null" class="pct">
                        {{ signedPct(r.deltaPct) }}
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="small muted">这个账期还没有算出损益表。</p>
          </n-spin>
          <p v-if="detailItems.some((r) => r.partial)" class="xs muted" style="margin-top: var(--s2)">
            带 <span class="warn">*</span> 的项不是所有店都有，鼠标停在星号上看是几家。
            费用项写成负数，和损益表一致。
          </p>
        </n-tab-pane>

        <n-tab-pane name="stores" :tab="`各店（${detailStores.length}）`">
          <div class="scroll tall">
            <table>
              <thead>
                <tr>
                  <th>店铺</th>
                  <th class="right">销售收入</th>
                  <th class="right">利润</th>
                  <th class="right">利润率</th>
                  <th class="right">比上月</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in detailStores" :key="c.store_id" class="tap" @click="open(c)">
                  <td class="nowrap">{{ c.store }}</td>
                  <td class="right num big-num">{{ money(c.revenue) }}</td>
                  <td class="right num big-num" :class="{ neg: c.profit < 0 }">
                    {{ money(c.profit) }}
                  </td>
                  <td class="right num">
                    {{ c.revenue ? percent(c.profit / c.revenue) : '—' }}
                  </td>
                  <td class="right num nowrap" :class="delta(c.delta)">
                    {{ signed(c.delta) }}
                    <span v-if="c.deltaPct !== null" class="pct">
                      {{ signedPct(c.deltaPct) }}
                    </span>
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
            </table>
          </div>
          <p class="xs muted" style="margin-top: var(--s2)">点一行进这家店这个月的账。</p>
        </n-tab-pane>
      </n-tabs>
    </n-modal>
  </n-spin>
</template>
