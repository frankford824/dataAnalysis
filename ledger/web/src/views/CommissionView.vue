<script setup>
/* 提成。
 *
 * 这一页只有三件事，所以就是三个标签：这个月要发多少、怎么配、现在配的是什么。
 * 上一版把它们竖着摊成一页，人打开先看到发放金额，滚到底才看到配置，中间还夹着
 * 一张预览表——「我现在该干嘛」这个问题一直没有答案。
 *
 * 配置这件事本身的顺序是固定的四步：谁拿几个点 → 哪些商品归谁 → 剩下的归谁 →
 * 看展开结果对不对 → 落库。系统知道的比人多（这个月卖过哪些商品、每个赚了多少、
 * 历史归属里它归谁管），所以前三步都预填好，人只要改。
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

//: 「谁拿几个点」。界面上填 3.5，这里存 3.5，发给后端前除以 100——
//: 把百分号翻译成小数这件事放在离人最近的地方。
const rates = ref({})
const extra = ref([])
const owners = ref({})
//: 店铺兜底可以是几个人分。淘宝那家店现在就是两个人分掉 5 个点。
const fallbacks = ref([])
const preview = ref(null)
const showPreview = ref(false)
const hunt = ref('')

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
  for (const s of plan.value?.stores || []) {
    for (const o of s.owners || []) {
      if (o.person && !(o.person in seen)) seen[o.person] = null
    }
  }
  rates.value = seen
  extra.value = []
  owners.value = {}

  // 店铺兜底就是现行规则里不带商品的那几条。把它读出来，人打开看到的是现状，
  // 而不是一张空表——空表会让人以为现在没有兜底，随手一存就把它抹了。
  const day = Object.values(since).sort().pop() || ''
  fallbacks.value = (config.value?.rules || [])
    .filter(
      (r) =>
        (!app.storeId || r.store === app.storeId) &&
        !r.product_id &&
        r.person &&
        (!day || r.effective_from === day),
    )
    .map((r) => ({ person: r.person, rate: +(Number(r.share) * 100).toFixed(3) }))
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

const products = computed(() => {
  const all = (plan.value?.products || []).filter((p) => p.store_id === storeId.value)
  const q = hunt.value.trim()
  if (!q) return all
  return all.filter(
    (p) => (p.product_name || '').includes(q) || (p.product_id || '').includes(q),
  )
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

const fallbackTotal = computed(() =>
  +fallbacks.value.reduce((a, f) => a + (Number(f.rate) || 0), 0).toFixed(3),
)

const fallbackLabel = computed(() => {
  const live = fallbacks.value.filter((f) => f.person && f.rate)
  if (!live.length) return '不给任何人'
  if (live.length === 1) return `${live[0].person} ${live[0].rate}%`
  return `${live.length} 人分 ${fallbackTotal.value}%`
})

const storeName = computed(() =>
  Object.fromEntries((config.value?.stores || []).map((s) => [s.id, s.name])),
)

const newcomer = ref('')

function addPerson() {
  const who = newcomer.value.trim()
  if (!who) return
  if (!(who in rates.value)) rates.value[who] = null
  if (!extra.value.includes(who)) extra.value.push(who)
  newcomer.value = ''
  message.success(`${who} 加进来了。给他一个点数，再到第二步把商品指给他。`)
}

/** 商品归属改了或者改回默认。改回默认就把这条覆盖删掉，别留一条和建议一样的。 */
function setOwner(productId, who) {
  if (who === null || who === undefined || who === '') delete owners.value[productId]
  else owners.value[productId] = who
}

function payload() {
  const out = {}
  for (const [person, v] of Object.entries(rates.value)) {
    if (v) out[person] = Number(v) / 100
  }
  const rest = {}
  for (const f of fallbacks.value) {
    if (f.person && f.rate) rest[f.person] = Number(f.rate) / 100
  }
  return {
    store_id: storeId.value,
    period: period.value,
    rates: out,
    owners: owners.value,
    fallbacks: rest,
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
              <div class="spread" style="margin-bottom: var(--s3)">
                <div class="small muted">
                  {{ app.currentStore?.name }} ·
                  这个月卖过 {{ count(products.length) }} 个商品 ·
                  生效日 {{ period }}-01
                </div>
                <div class="row">
                  <n-button size="small" @click="look">看展开结果</n-button>
                </div>
              </div>

              <n-alert v-if="stale" type="warning" :bordered="false" style="margin-bottom: var(--s3)">
                商品归属数据只到 {{ stale }}，下面的归属是沿用那时的安排。人换了的话在第二步改。
              </n-alert>

              <n-collapse :default-expanded-names="['who']" accordion>
                <n-collapse-item name="who" title="一、谁拿几个点">
                  <template #header-extra>
                    <span class="xs muted">{{ staff.length }} 人</span>
                  </template>
                  <div class="scroll">
                    <n-table size="small" :bordered="false">
                      <thead>
                        <tr>
                          <th>人</th>
                          <th class="right">管着的商品</th>
                          <th class="right">这个月{{ pay?.base_name || '利润' }}</th>
                          <th style="width: 130px">拿几个点</th>
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
                            <span v-else class="xs muted">第三步的兜底管这些</span>
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
                      历史归属里没有的人从这里加，然后到第二步把商品指给他。
                    </span>
                  </div>
                </n-collapse-item>

                <n-collapse-item name="what" title="二、哪些商品归谁">
                  <template #header-extra>
                    <span class="xs muted">
                      {{ changed ? `改了 ${changed} 个` : '按历史归属' }}
                    </span>
                  </template>
                  <div class="spread" style="margin-bottom: var(--s3)">
                    <p class="xs muted">
                      默认沿用历史归属，不用逐个填。只在人换了的时候改这里；
                      改成「不单独配」就交给第三步兜底。
                    </p>
                    <n-input
                      v-model:value="hunt"
                      size="small"
                      clearable
                      placeholder="找商品：名称或 ID"
                      style="width: 220px"
                    />
                  </div>
                  <div class="scroll tall">
                    <n-table size="small" :bordered="false">
                      <thead>
                        <tr>
                          <th>商品</th>
                          <th class="right">本月{{ pay?.base_name || '利润' }}</th>
                          <th style="width: 190px">归谁</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="p in products.slice(0, 300)" :key="p.product_id">
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
                              :options="[...people, { label: '不单独配（走兜底）', value: '-' }]"
                              :placeholder="p.suggest_person || '没人管'"
                              @update:value="(v) => setOwner(p.product_id, v)"
                            />
                          </td>
                        </tr>
                      </tbody>
                    </n-table>
                  </div>
                  <p v-if="products.length > 300" class="xs muted" style="margin-top: var(--s2)">
                    只列了利润最高的 300 个。长尾商品逐个配没有意义，交给兜底。
                  </p>
                </n-collapse-item>

                <n-collapse-item name="rest" title="三、剩下没人管的归谁">
                  <template #header-extra>
                    <span class="xs muted">{{ fallbackLabel }}</span>
                  </template>
                  <p class="xs muted" style="margin-bottom: var(--s3)">
                    长尾商品逐个配没有意义，交给按店兜底。可以几个人分——这里填的点数
                    加起来就是这家店的总提成率。一个人都不填就是这部分不给任何人。
                  </p>
                  <div class="stack">
                    <div v-for="(f, i) in fallbacks" :key="i" class="row">
                      <n-select
                        v-model:value="f.person"
                        size="small"
                        filterable
                        tag
                        :options="people"
                        placeholder="人名"
                        style="width: 200px"
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
                      <n-button size="tiny" quaternary type="error" @click="fallbacks.splice(i, 1)">
                        去掉
                      </n-button>
                    </div>
                  </div>
                  <div class="row" style="margin-top: var(--s3)">
                    <n-button size="tiny" @click="fallbacks.push({ person: '', rate: null })">
                      加一个人
                    </n-button>
                    <span v-if="fallbacks.length > 1" class="xs muted num">
                      合计 {{ fallbackTotal }}%
                    </span>
                  </div>
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
