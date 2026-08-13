<script setup>
/* 提成。
 *
 * 这一页只有三件事，所以就是三个标签：这个月要发多少、怎么配、现在配的是什么。
 * 上一版把它们竖着摊成一页，人打开先看到发放金额，滚到底才看到配置，中间还夹着
 * 一张预览表——「我现在该干嘛」这个问题一直没有答案。
 *
 * 配置按的是「一个商品的总提成率定死，再分给几个人」这个口径，所以分法只有
 * 两种角色：运营（谁管这个商品谁拿那一格）和固定分成（主管助理那类，每个商品
 * 都分一份）。两者相加就是这家店的总提成率，页面上一直写着它是多少——加一个人
 * 却看不出总数变成几个点，是上一版最要命的地方。
 *
 * 顺序：定运营的点数 → 定谁管哪些商品 → 定固定分成 → 看展开结果 → 落库。
 * 系统知道的比人多（这个月卖过哪些商品、每个赚了多少、历史归属里它归谁管），
 * 所以每一步都预填好，人只要改。
 *
 * 猜测永远只是猜测，它不进计算。进计算的是提成配置本身，也就是第三个标签里
 * 那张表，每一行人都能看见。
 */
import { useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import { count, money, percent } from '../format'
import { useApp } from '../store'

const app = useApp()
const message = useMessage()
const router = useRouter()

const pay = ref(null)
const plan = ref(null)
const config = ref(null)
const loading = ref(false)
const failed = ref('')
const tab = ref('payout')

//: 运营各自几个点。界面上填 3.5，这里存 3.5，发给后端前除以 100——
//: 把百分号翻译成小数这件事放在离人最近的地方。
const rates = ref({})
const extra = ref([])
const owners = ref({})
//: 每个商品都分一份的人：主管、助理那类，不看商品归谁。
const fixed = ref([])
//: 没有归属的商品，运营那一格归谁。
const fallbackOwner = ref('')
const preview = ref(null)
const showPreview = ref(false)
const hunt = ref('')
const onlyOwner = ref(null)
const bulk = ref(null)
const step = ref('who')

// 配置只认筛选条里明确选的那家店。默认落到第一家的话，页面上没有任何地方写着
// 「你正在配的是哪家」，而配错店这件事要等到发钱那天才看得出来。
const storeId = computed(() => app.storeId)
const period = computed(() => app.period || pay.value?.period || '')

async function load() {
  loading.value = true
  failed.value = ''
  preview.value = null
  try {
    pay.value = await api.commission(period.value)
    plan.value = await api.commissionProducts({
      period: period.value,
      store_id: app.storeId,
    }).catch(() => null)
    config.value = await api.commissionConfig(app.storeId).catch(() => null)
    seed()
  } catch (e) {
    failed.value = e.message
  } finally {
    loading.value = false
  }
}

/** 用现行配置和归属建议把费率框填上，人只需要改。
 *
 * 点数从现行规则里读，不从发放结果里读：发放结果只有金额，倒推回费率会碰上
 * 亏损单不倒扣那类政策，推出来的数和配置里写的对不上。同一个人有多条时取生效
 * 日期最新的那条——那就是「现在是几个点」。
 */
function seed() {
  const seen = {}
  const since = {}
  for (const r of config.value?.rules || []) {
    if (app.storeId && r.store !== app.storeId) continue
    if (!r.person) continue
    if (since[r.person] && since[r.person] > r.effective_from) continue
    since[r.person] = r.effective_from
    seen[r.person] = +(Number(r.share) * 100).toFixed(3)
  }

  // 店铺那一组（不带商品号的规则）是现在的默认分法。把它拆回两种角色：在商品
  // 归属里管着东西的那个是运营，其余的是固定分成。淘宝那两条正是这样——汪学成
  // 管着 627 个商品，李秋雨一个也不管，她那 1.5 个点是每个商品都分的。
  const owns = new Set()
  for (const s of plan.value?.stores || []) {
    for (const o of s.owners || []) if (o.person) owns.add(o.person)
  }
  const day = Object.values(since).sort().pop() || ''
  const storeRows = (config.value?.rules || []).filter(
    (r) =>
      (!app.storeId || r.store === app.storeId) &&
      !r.product_id &&
      r.person &&
      (!day || r.effective_from === day),
  )

  fixed.value = []
  fallbackOwner.value = ''
  for (const r of storeRows) {
    const rate = +(Number(r.share) * 100).toFixed(3)
    if (owns.has(r.person) && !fallbackOwner.value) {
      fallbackOwner.value = r.person
      seen[r.person] = rate
    } else {
      fixed.value.push({ person: r.person, rate })
    }
  }

  for (const person of owns) if (!(person in seen)) seen[person] = null
  rates.value = seen
  extra.value = []
  owners.value = {}
}

watch(() => [app.period, app.storeId], load, { immediate: true })

/** 这家店里系统认得出负责人的商品，按人汇总。加进来的新人排在后面。 */
const staff = computed(() => {
  const s = (plan.value?.stores || []).find((x) => x.store_id === storeId.value)
  const known = s?.owners || []
  const names = new Set(known.map((o) => o.person))
  return [
    ...known,
    ...extra.value.filter((n) => !names.has(n)).map((n) => ({ person: n, products: 0, base: 0, fresh: true })),
  ]
})

/** 人名下拉的选项。配过的、猜出来的、刚加的，都算。 */
const people = computed(() => {
  const names = new Set(Object.keys(rates.value))
  for (const r of config.value?.rules || []) if (r.person) names.add(r.person)
  return [...names].filter(Boolean).map((n) => ({ label: n, value: n }))
})

const products = computed(() =>
  (plan.value?.products || []).filter((p) => p.store_id === storeId.value),
)

/** 这个商品现在归谁——人改过就是改后的，没改过就是系统猜的。 */
function ownerOf(p) {
  const set = owners.value[p.product_id]
  if (set === '-') return ''
  return set || p.suggest_person || ''
}

const ownerFilters = computed(() => {
  const by = new Map()
  for (const p of products.value) {
    const who = ownerOf(p) || '(没人管)'
    by.set(who, (by.get(who) || 0) + 1)
  }
  return [...by].map(([who, n]) => ({ label: `${who} · ${n} 个`, value: who }))
})

/** 搜索加「按现在归谁」筛。七百个商品逐个点没人做得完，得能一批一批指。 */
const shownProducts = computed(() => {
  const q = hunt.value.trim()
  return products.value.filter((p) => {
    if (onlyOwner.value && (ownerOf(p) || '(没人管)') !== onlyOwner.value) return false
    if (!q) return true
    return (p.product_name || '').includes(q) || (p.product_id || '').includes(q)
  })
})

// 只按筛选条里明确选的店过滤。配置那一步没选店会默认落到第一家，但「现在配的是
// 什么」这张表跟着默认走的话，人看到的是一家店的规则、以为那就是全部。
const rules = computed(() =>
  (config.value?.rules || []).filter((r) => !app.storeId || r.store === app.storeId),
)

// 筛选条选了店，发放也跟着只看这家。选了一家店却看到别家的人拿了多少钱，人会
// 以为这些钱是这家店出的。按人那一栏因此改用店内明细，而不是跨店汇总。
const payStore = computed(() =>
  (pay.value?.stores || []).find((s) => s.store_id === app.storeId) || null,
)
const payPeople = computed(() =>
  app.storeId ? payStore.value?.people || [] : pay.value?.people || [],
)
const payStores = computed(() =>
  (pay.value?.stores || []).filter((s) => !app.storeId || s.store_id === app.storeId),
)
const payTotal = computed(() =>
  app.storeId ? payPeople.value.reduce((a, p) => a + (p.amount || 0), 0) : pay.value?.total,
)

const stale = computed(() => {
  const latest = plan.value?.ownership_latest
  return latest && plan.value?.period && latest < plan.value.period ? latest : ''
})

const changed = computed(() => Object.keys(owners.value).length)

/** 固定分成合计。每个商品都要分掉这么多，跟归谁管没关系。 */
const fixedTotal = computed(() =>
  +fixed.value.reduce((a, f) => a + (f.person ? Number(f.rate) || 0 : 0), 0).toFixed(3),
)

/** 这个人管的商品，总提成率是几个点。人真正要确认的就是这一列。 */
function totalFor(person) {
  return +((Number(rates.value[person]) || 0) + fixedTotal.value).toFixed(3)
}

const fixedLabel = computed(() => {
  const live = fixed.value.filter((f) => f.person && f.rate)
  if (!live.length) return '没有'
  if (live.length === 1) return `${live[0].person} ${live[0].rate}%`
  return `${live.length} 人共 ${fixedTotal.value}%`
})

const ownerLabel = computed(() =>
  fallbackOwner.value
    ? `${fallbackOwner.value} ${rates.value[fallbackOwner.value] || 0}%`
    : '运营那一格空着',
)

/** 被指到商品上、却没定点数的人。不说出来的话，他名下的商品一分钱都不发。 */
const unpaid = computed(() => {
  const used = new Set(Object.values(owners.value).filter((w) => w && w !== '-'))
  for (const o of staff.value) if (o.person && o.products) used.add(o.person)
  if (fallbackOwner.value) used.add(fallbackOwner.value)
  return [...used].filter((w) => !rates.value[w])
})

/** 大多数商品的总提成率：兜底运营那一格 + 固定分成。页头一直挂着它。 */
const defaultTotal = computed(() =>
  fallbackOwner.value ? totalFor(fallbackOwner.value) : fixedTotal.value,
)

const storeName = computed(() =>
  Object.fromEntries((config.value?.stores || []).map((s) => [s.id, s.name])),
)

const newcomer = ref('')

/** 把一个名字登记进这一页，之后所有下拉里都有他。
 *
 * 任何一个选人的地方都能直接打名字新建，但新建出来的名字如果不落进费率表，
 * 他就是个没有点数的人——指给他的商品最后一分钱都不发，而页面上看着是配好的。
 * 所以新建走这里，一律先占一个空点数的位置，第一步那张表会把它显示成待填。
 */
function register(who) {
  const name = (who || '').trim()
  if (!name || name === '-') return name
  if (!(name in rates.value)) rates.value[name] = null
  if (!extra.value.includes(name)) extra.value.push(name)
  return name
}

function addPerson() {
  const who = register(newcomer.value)
  if (!who) return
  newcomer.value = ''
  // 加完人光弹一句提示没用——人还得自己找到第二步在哪。直接把第二步打开，
  // 他要做的下一件事就摆在眼前。
  step.value = 'what'
  message.success(`${who} 加进来了。给他一个点数，再在下面把商品指给他。`)
}

/** 商品归属改了或者改回默认。改回默认就把这条覆盖删掉，别留一条和建议一样的。 */
function setOwner(productId, who) {
  if (who === null || who === undefined || who === '') delete owners.value[productId]
  else owners.value[productId] = register(who)
}

/** 把筛出来的这一批一起指给某个人。逐个点七百次这件事没人会做。 */
function assignAll() {
  const who = register(bulk.value)
  if (!who) return
  const hit = shownProducts.value
  for (const p of hit) owners.value[p.product_id] = who
  message.success(`${hit.length} 个商品指给了 ${who}`)
  bulk.value = null
  if (rates.value[who] == null) step.value = 'who'
}

/** 第三步的固定分成、第四步的兜底运营，选到新名字也一样要登记。 */
function pickPerson(target, key, who) {
  target[key] = register(who) || ''
}

function payload() {
  const out = {}
  for (const [person, v] of Object.entries(rates.value)) {
    if (v) out[person] = Number(v) / 100
  }
  const share = {}
  for (const f of fixed.value) {
    if (f.person && f.rate) share[f.person] = Number(f.rate) / 100
  }
  return {
    store_id: storeId.value,
    period: period.value,
    rates: out,
    owners: owners.value,
    fixed: share,
    fallback_owner: fallbackOwner.value,
  }
}

async function look() {
  try {
    preview.value = await api.commissionPlan(payload(), false)
    showPreview.value = true
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

async function apply() {
  try {
    const res = await app.run('正在展开并重算', () => api.commissionPlan(payload(), true))
    preview.value = res
    showPreview.value = false
    app.invalidate()
    await load()
    tab.value = 'rules'
    message.success(`配好了 ${res.generated} 条规则`)
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

async function upload(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  try {
    const res = await app.run('正在收提成配置', () => api.uploadCommission(file, true))
    message.success(`收下了 ${res.count} 条规则`)
    await load()
  } catch (err) {
    message.error(err.message, { duration: 6000 })
  }
}

function open(id) {
  app.pick({ store: id })
  router.push({ name: 'period', params: { id }, query: { period: period.value } })
}
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="failed" type="error" :bordered="false">{{ failed }}</n-alert>

    <template v-else>
      <div class="spread" style="margin-bottom: var(--s4)">
        <div>
          <h1>提成</h1>
          <div class="small muted">
            {{ period }} · 按{{ pay?.base_name || '利润' }}算
            <template v-if="pay?.base_mixed"> · 各店口径不一样</template>
          </div>
        </div>
        <div class="row">
          <span class="small muted num">合计 {{ money(payTotal) }}</span>
          <n-button size="small" type="primary" @click="tab = 'config'">配提成</n-button>
        </div>
      </div>

      <div class="card">
        <n-tabs v-model:value="tab" type="line" size="small">
          <!-- 1. 这个月要发多少 -->
          <n-tab-pane name="payout" tab="本月发放">
            <div class="cols even">
              <div>
                <div class="spread" style="margin-bottom: var(--s3)">
                  <h3>按人</h3>
                  <span class="small muted num">{{ money(payTotal) }}</span>
                </div>
                <div v-if="payPeople.length" class="scroll">
                  <n-table size="small" :bordered="false">
                    <thead>
                      <tr>
                        <th>人</th>
                        <th class="right">提成</th>
                        <th class="right">基数</th>
                        <th class="right">商品数</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="p in payPeople" :key="p.person">
                        <td>{{ p.person }}</td>
                        <td class="right num">{{ money(p.amount) }}</td>
                        <td class="right num">{{ money(p.base) }}</td>
                        <td class="right num">{{ count(p.products) }}</td>
                      </tr>
                    </tbody>
                  </n-table>
                </div>
                <n-empty v-else description="这个月没有人拿到提成" size="small">
                  <template #extra>
                    <n-button size="small" @click="tab = 'config'">去配</n-button>
                  </template>
                </n-empty>
              </div>

              <div>
                <h3 style="margin-bottom: var(--s3)">按店</h3>
                <div v-if="payStores.length" class="scroll">
                  <n-table size="small" :bordered="false">
                    <thead>
                      <tr>
                        <th>店铺</th>
                        <th class="right">基数</th>
                        <th class="right">提成</th>
                        <th>说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="s in payStores" :key="s.store_id">
                        <td>
                          <button class="link" @click="open(s.store_id)">{{ s.store }}</button>
                        </td>
                        <td class="right num">{{ money(s.base_total) }}</td>
                        <td class="right num">{{ money(s.total) }}</td>
                        <td class="xs muted">
                          <template v-if="!s.configured">还没配提成</template>
                          <template v-else-if="s.unassigned_base">有商品没人管</template>
                          <template v-else-if="s.on_loss === 'skip'">亏损单不倒扣</template>
                          <template v-else>—</template>
                        </td>
                      </tr>
                    </tbody>
                  </n-table>
                </div>
                <n-empty v-else description="这个账期还没有算过的店" size="small" />
              </div>
            </div>
          </n-tab-pane>

          <!-- 2. 配 -->
          <n-tab-pane name="config" tab="配提成">
            <n-alert v-if="!storeId" type="default" :bordered="false">
              上面的筛选条里先选一家店。提成是按店配的——同一个人在不同店的点数可以不一样，
              所以这一步必须说清楚是哪家。
            </n-alert>

            <template v-else>
              <div class="spread wrap" style="margin-bottom: var(--s3)">
                <div class="small muted">
                  {{ app.currentStore?.name }} ·
                  这个月卖过 {{ count(products.length) }} 个商品 ·
                  生效日 {{ period }}-01
                </div>
                <div class="row">
                  <span class="small">
                    默认总提成率
                    <span class="num strong">{{ defaultTotal }}%</span>
                    <span class="xs muted">
                      （{{ ownerLabel }}
                      <template v-if="fixedTotal"> + 固定 {{ fixedTotal }}%</template>）
                    </span>
                  </span>
                  <n-button size="small" @click="look">看展开结果</n-button>
                </div>
              </div>

              <n-alert v-if="stale" type="warning" :bordered="false" style="margin-bottom: var(--s3)">
                商品归属数据只到 {{ stale }}，下面的归属是沿用那时的安排。人换了的话在第二步改。
              </n-alert>

              <n-collapse v-model:expanded-names="step" accordion>
                <n-collapse-item name="who" title="一、运营各拿几个点">
                  <template #header-extra>
                    <span class="xs" :class="unpaid.length ? 'warn' : 'muted'">
                      {{ unpaid.length ? `${unpaid.join('、')} 还没定点数` : `${staff.length} 人` }}
                    </span>
                  </template>
                  <p class="xs muted" style="margin-bottom: var(--s3)">
                    运营是「谁管这个商品谁拿这一格」。他管的商品的总提成率，
                    就是这一格加上第三步的固定分成。
                  </p>
                  <div class="scroll">
                    <n-table size="small" :bordered="false">
                      <thead>
                        <tr>
                          <th>人</th>
                          <th class="right">管着的商品</th>
                          <th class="right">这个月{{ pay?.base_name || '利润' }}</th>
                          <th style="width: 130px">运营这一格</th>
                          <th class="right">他管的商品总提成率</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="o in staff" :key="o.person || '(没人管)'">
                          <td>
                            {{ o.person || '（系统认不出负责人）' }}
                            <n-tag v-if="o.fresh" size="tiny" type="info" :bordered="false">新加</n-tag>
                          </td>
                          <td class="right num">{{ count(o.products) }}</td>
                          <td class="right num" :class="{ neg: o.base < 0 }">{{ money(o.base) }}</td>
                          <td>
                            <n-input-number
                              v-if="o.person"
                              v-model:value="rates[o.person]"
                              size="small"
                              :min="0"
                              :max="100"
                              :step="0.5"
                              placeholder="0"
                            >
                              <template #suffix>%</template>
                            </n-input-number>
                            <span v-else class="xs muted">第四步说这些归谁</span>
                          </td>
                          <td class="right num">
                            <template v-if="o.person">{{ totalFor(o.person) }}%</template>
                            <span v-else class="na">—</span>
                          </td>
                        </tr>
                      </tbody>
                    </n-table>
                  </div>
                  <div class="row" style="margin-top: var(--s3)">
                    <n-input
                      v-model:value="newcomer"
                      size="small"
                      placeholder="新来的人叫什么"
                      style="width: 180px"
                      @keyup.enter="addPerson"
                    />
                    <n-button size="tiny" :disabled="!newcomer.trim()" @click="addPerson">
                      加进来
                    </n-button>
                    <span class="xs muted">
                      历史归属里没有的人从这里加，加完直接进第二步指商品。
                      下面每个选人的框也都能直接打名字新建。
                    </span>
                  </div>
                </n-collapse-item>

                <n-collapse-item name="what" title="二、谁管哪些商品">
                  <template #header-extra>
                    <span class="xs muted">
                      {{ changed ? `改了 ${changed} 个` : '按历史归属' }}
                    </span>
                  </template>
                  <div class="spread wrap" style="margin-bottom: var(--s3)">
                    <p class="xs muted">
                      默认沿用历史归属，不用逐个填。只在人换了的时候改这里——
                      改的是运营那一格归谁，固定分成不受影响。
                    </p>
                    <div class="row">
                      <n-select
                        v-model:value="onlyOwner"
                        size="small"
                        clearable
                        :options="ownerFilters"
                        placeholder="按现在归谁筛"
                        style="width: 180px"
                      />
                      <n-input
                        v-model:value="hunt"
                        size="small"
                        clearable
                        placeholder="找商品：名称或 ID"
                        style="width: 200px"
                      />
                    </div>
                  </div>
                  <div v-if="shownProducts.length" class="row" style="margin-bottom: var(--s3)">
                    <span class="xs muted">
                      这 {{ count(shownProducts.length) }} 个商品一起指给
                    </span>
                    <n-select
                      v-model:value="bulk"
                      size="small"
                      filterable
                      tag
                      clearable
                      :options="people"
                      placeholder="选人／打名字新建"
                      style="width: 190px"
                    />
                    <n-button size="tiny" :disabled="!bulk" @click="assignAll">
                      一起指过去
                    </n-button>
                  </div>
                  <div class="scroll tall">
                    <n-table size="small" :bordered="false">
                      <thead>
                        <tr>
                          <th>商品</th>
                          <th class="right">本月{{ pay?.base_name || '利润' }}</th>
                          <th style="width: 190px">谁管</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="p in shownProducts.slice(0, 300)" :key="p.product_id">
                          <td class="xs">
                            {{ p.product_name || p.product_id }}
                            <div class="xs muted num">{{ p.product_id }}</div>
                          </td>
                          <td class="right num xs" :class="{ neg: p.base < 0 }">
                            {{ money(p.base) }}
                          </td>
                          <td>
                            <n-select
                              size="small"
                              filterable
                              tag
                              clearable
                              :value="owners[p.product_id] ?? (p.suggest_person || null)"
                              :options="[...people, { label: '没人管（只发固定分成）', value: '-' }]"
                              placeholder="打名字可新建"
                              @update:value="(v) => setOwner(p.product_id, v)"
                            />
                          </td>
                        </tr>
                      </tbody>
                    </n-table>
                  </div>
                  <p v-if="shownProducts.length > 300" class="xs muted" style="margin-top: var(--s2)">
                    列的是利润最高的 300 个。剩下的用上面的筛选一批一批指，
                    或者交给第四步。
                  </p>
                </n-collapse-item>

                <n-collapse-item name="fixed" title="三、每个商品都分一份的人">
                  <template #header-extra>
                    <span class="xs muted">{{ fixedLabel }}</span>
                  </template>
                  <p class="xs muted" style="margin-bottom: var(--s3)">
                    主管、助理这类，不看商品归谁管，每个商品都分一份。这里填的点数
                    加进每个商品的总提成率里。
                  </p>
                  <div class="stack">
                    <div v-for="(f, i) in fixed" :key="i" class="row">
                      <n-select
                        :value="f.person || null"
                        size="small"
                        filterable
                        tag
                        :options="people"
                        placeholder="人名／打名字新建"
                        style="width: 200px"
                        @update:value="(v) => pickPerson(f, 'person', v)"
                      />
                      <n-input-number
                        v-model:value="f.rate"
                        size="small"
                        :min="0"
                        :max="100"
                        :step="0.5"
                        placeholder="几个点"
                        style="width: 140px"
                      >
                        <template #suffix>%</template>
                      </n-input-number>
                      <n-button size="tiny" quaternary type="error" @click="fixed.splice(i, 1)">
                        去掉
                      </n-button>
                    </div>
                  </div>
                  <div class="row" style="margin-top: var(--s3)">
                    <n-button size="tiny" @click="fixed.push({ person: '', rate: null })">
                      加一个人
                    </n-button>
                    <span v-if="fixed.length" class="xs muted num">合计 {{ fixedTotal }}%</span>
                  </div>
                </n-collapse-item>

                <n-collapse-item name="rest" title="四、没人管的商品，运营那一格归谁">
                  <template #header-extra>
                    <span class="xs muted">{{ ownerLabel }}</span>
                  </template>
                  <p class="xs muted" style="margin-bottom: var(--s3)">
                    长尾商品逐个指没有意义。指一个人接住它们，留空就是这一格不给人——
                    那些商品只发固定分成。
                  </p>
                  <n-select
                    :value="fallbackOwner || null"
                    size="small"
                    filterable
                    tag
                    clearable
                    :options="people"
                    placeholder="选人／打名字新建，留空表示不给"
                    style="width: 260px"
                    @update:value="(v) => (fallbackOwner = register(v) || '')"
                  />
                </n-collapse-item>
              </n-collapse>

              <div class="row" style="margin-top: var(--s4)">
                <n-button size="small" type="primary" @click="look">看展开结果</n-button>
                <span class="xs muted">
                  先看会写进配置的每一行，确认了再落库。落库会把这家店重算一遍。
                </span>
              </div>
            </template>
          </n-tab-pane>

          <!-- 3. 现在配的是什么 -->
          <n-tab-pane name="rules" :tab="`现行规则（${rules.length}）`">
            <div class="spread" style="margin-bottom: var(--s3)">
              <p class="xs muted">
                真正参与计算的就是这张表。上面那一步做的事，就是往这里写行。
                同一个商品有多条时，按生效日期取当天之前最近的一条。
              </p>
              <div class="row">
                <n-button size="small" tag="a" href="/api/commission/config.csv">
                  导出 CSV
                </n-button>
                <n-button size="small" @click="$refs.picker.click()">导入 CSV</n-button>
                <input ref="picker" type="file" accept=".csv,.xlsx,.xls,.xlsm" hidden @change="upload" />
              </div>
            </div>
            <n-alert type="default" :bordered="false" style="margin-bottom: var(--s3)">
              <span class="xs">
                导出是把这张表下载成 Excel 能打开的 CSV，拿去批量改；导入是把改完的整份传回来，
                <strong>整表覆盖</strong>——传上去之后这张表就等于那份文件，不是往里追加。
                只改几个人的话，用上一个标签页更快。
              </span>
            </n-alert>
            <div v-if="rules.length" class="scroll tall">
              <n-table size="small" :bordered="false">
                <thead>
                  <tr>
                    <th>生效日</th>
                    <th>店铺</th>
                    <th>商品</th>
                    <th>人</th>
                    <th class="right">费率</th>
                    <th>备注</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, i) in rules" :key="i">
                    <td class="xs num nowrap">{{ r.effective_from }}</td>
                    <td class="xs">{{ storeName[r.store] || r.store }}</td>
                    <td class="xs">{{ r.product_name || r.product_id || '（店铺兜底）' }}</td>
                    <td class="xs">{{ r.person }}</td>
                    <td class="right num xs">{{ percent(Number(r.share)) }}</td>
                    <td class="xs muted">{{ r.note }}</td>
                  </tr>
                </tbody>
              </n-table>
            </div>
            <n-empty v-else description="这家店还没有提成规则" size="small">
              <template #extra>
                <n-button size="small" @click="tab = 'config'">去配</n-button>
              </template>
            </n-empty>
          </n-tab-pane>
        </n-tabs>
      </div>

      <n-modal
        v-model:show="showPreview"
        preset="card"
        title="会写进配置的东西"
        style="max-width: 760px"
      >
        <p class="small" style="margin-bottom: var(--s3)">
          {{ count(preview?.generated) }} 条规则。
          {{ count(preview?.coverage?.by_product || 0) }} 个商品单独配，
          {{ count(preview?.coverage?.by_store || 0) }} 个走店铺兜底，
          {{ count(preview?.coverage?.nobody || 0) }} 个不给任何人。
          生效日 {{ preview?.effective_from }}，同一天的旧配置会被这一份换掉。
          <template v-if="(preview?.generated || 0) > (preview?.preview?.length || 0)">
            下面列的是前 {{ preview.preview.length }} 条。
          </template>
        </p>
        <div class="scroll tall">
          <n-table size="small" :bordered="false">
            <thead>
              <tr>
                <th>商品</th>
                <th>人</th>
                <th class="right">费率</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(r, i) in preview?.preview || []" :key="i">
                <td class="xs">{{ r.product_name || r.product_id || '（店铺兜底）' }}</td>
                <td class="xs">{{ r.person }}</td>
                <td class="right num xs">{{ percent(Number(r.share)) }}</td>
              </tr>
            </tbody>
          </n-table>
        </div>
        <template #footer>
          <div class="row" style="justify-content: flex-end">
            <n-button size="small" @click="showPreview = false">再改改</n-button>
            <n-button size="small" type="primary" @click="apply">落库并重算</n-button>
          </div>
        </template>
      </n-modal>
    </template>
  </n-spin>
</template>
