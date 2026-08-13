<script setup>
/* 提成。
 *
 * 上一版把配置做成了一张空表加一个上传框，人打开只会问「我该填什么」。可系统这边
 * 知道的比人多：这个账期卖过哪些商品、每个赚了多少、历史归属里它归谁管。所以这一页
 * 反过来——先把系统猜的摆出来，人只回答一个问题：谁拿几个点。
 *
 * 猜测永远只是猜测，它不进计算。进计算的是提成配置本身，展开之后人能看见每一行。
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
const loading = ref(false)
const failed = ref('')

//: 「谁拿几个点」。界面上填 3.5，这里存 3.5，发给后端前除以 100——
//: 把百分号翻译成小数这件事放在离人最近的地方。
const rates = ref({})
const fallbackPerson = ref('')
const fallbackRate = ref(null)
const preview = ref(null)
const uploading = ref(null)

const storeId = computed(() => app.storeId || plan.value?.stores?.[0]?.store_id || '')
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
    seed()
  } catch (e) {
    failed.value = e.message
  } finally {
    loading.value = false
  }
}

/** 用现行配置和归属建议把费率框填上，人只需要改。 */
function seed() {
  const seen = {}
  for (const p of pay.value?.people || []) {
    if (p.rate) seen[p.person] = +(p.rate * 100).toFixed(3)
  }
  for (const s of plan.value?.stores || []) {
    for (const o of s.owners || []) {
      if (o.person && !(o.person in seen)) seen[o.person] = null
    }
  }
  rates.value = seen
}

watch(() => [app.period, app.storeId], load, { immediate: true })

/** 这家店里系统认得出负责人的商品，按人汇总。 */
const owners = computed(() => {
  const s = (plan.value?.stores || []).find((x) => x.store_id === storeId.value)
  return s?.owners || []
})

const guessed = computed(() =>
  (plan.value?.products || []).filter((p) => p.store_id === storeId.value),
)

const stale = computed(() => {
  const latest = plan.value?.ownership_latest
  return latest && plan.value?.period && latest < plan.value.period ? latest : ''
})

function payload(apply) {
  const out = {}
  for (const [person, v] of Object.entries(rates.value)) {
    if (v) out[person] = Number(v) / 100
  }
  return {
    store_id: storeId.value,
    period: period.value,
    rates: out,
    fallback_person: fallbackPerson.value,
    fallback_rate: fallbackRate.value ? Number(fallbackRate.value) / 100 : 0,
  }
}

async function look() {
  try {
    preview.value = await api.commissionPlan(payload(false), false)
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

async function apply() {
  try {
    const res = await app.run('正在展开并重算', () => api.commissionPlan(payload(true), true))
    preview.value = res
    app.invalidate()
    await load()
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
          <n-button size="small" tag="a" href="/api/commission/config.csv">下载现行配置</n-button>
          <n-button size="small" @click="$refs.picker.click()">传一份配置</n-button>
          <input ref="picker" type="file" accept=".csv,.xlsx,.xls,.xlsm" hidden @change="upload" />
        </div>
      </div>

      <div class="card">
        <header>
          <h2>这个月要发多少</h2>
          <span class="sub">合计 <span class="num">{{ money(pay?.total) }}</span></span>
        </header>
        <n-table v-if="pay?.people?.length" size="small" :bordered="false">
          <thead>
            <tr>
              <th>人</th>
              <th class="right">提成</th>
              <th class="right">基数</th>
              <th class="right">商品数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in pay.people" :key="p.person">
              <td>{{ p.person }}</td>
              <td class="right num">{{ money(p.amount) }}</td>
              <td class="right num">{{ money(p.base) }}</td>
              <td class="right num">{{ count(p.products) }}</td>
            </tr>
          </tbody>
        </n-table>
        <n-empty v-else description="这个月没有人拿到提成">
          <template #extra>
            <div class="small muted" style="max-width: 420px">
              下面填上谁拿几个点，系统会照着商品归属展开成配置。
            </div>
          </template>
        </n-empty>
      </div>

      <div v-if="pay?.stores?.length" class="card">
        <header><h2>按店</h2></header>
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
            <tr v-for="s in pay.stores" :key="s.store_id">
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

      <div class="card">
        <header>
          <h2>配提成</h2>
          <span class="sub">
            {{ app.currentStore?.name || '选一家店' }}
            <template v-if="guessed.length">
              · 这个月卖过 {{ count(guessed.length) }} 个商品
            </template>
          </span>
        </header>

        <n-alert v-if="!storeId" type="default" :bordered="false">
          上面的筛选条里先选一家店。提成是按店配的——同一个人在不同店的点数可以不一样。
        </n-alert>

        <template v-else>
          <n-alert v-if="stale" type="warning" :bordered="false" style="margin-bottom: var(--s4)">
            商品归属数据只到 {{ stale }}，下面的建议是沿用那时的安排。人换了的话要自己改。
          </n-alert>

          <p class="small muted" style="margin-bottom: var(--s3)">
            系统已经知道每个商品归谁管，你只要回答谁拿几个点。填完先看展开结果，确认了再落库。
          </p>

          <n-table size="small" :bordered="false">
            <thead>
              <tr>
                <th>人</th>
                <th class="right">管着的商品</th>
                <th class="right">这个月{{ pay?.base_name || '利润' }}</th>
                <th style="width: 140px">拿几个点</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="o in owners" :key="o.person || '(没人管)'">
                <td>{{ o.person || '（系统认不出负责人）' }}</td>
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
                  <span v-else class="xs muted">下面的兜底管这些</span>
                </td>
              </tr>
            </tbody>
          </n-table>

          <div class="panel">
            <h3>没人管的商品归谁</h3>
            <p class="small muted" style="margin-bottom: var(--s3)">
              长尾商品逐个配没有意义。指一个人按店兜底，留空就是这部分不给任何人。
            </p>
            <div class="row wrap">
              <n-input
                v-model:value="fallbackPerson"
                size="small"
                placeholder="人名，留空表示不给"
                style="width: 200px"
              />
              <n-input-number
                v-model:value="fallbackRate"
                size="small"
                :min="0"
                :max="100"
                :step="0.5"
                placeholder="几个点"
                style="width: 140px"
              >
                <template #suffix>%</template>
              </n-input-number>
            </div>
          </div>

          <div class="row" style="margin-top: var(--s5)">
            <n-button size="small" @click="look">先看展开结果</n-button>
            <n-button size="small" type="primary" :disabled="!preview" @click="apply">
              落库并重算
            </n-button>
          </div>

          <div v-if="preview" class="panel">
            <h3>会写进配置的东西</h3>
            <p class="small">
              {{ count(preview.generated) }} 条规则。
              {{ count(preview.coverage?.by_product || 0) }} 个商品单独配，
              {{ count(preview.coverage?.by_store || 0) }} 个走店铺兜底，
              {{ count(preview.coverage?.nobody || 0) }} 个不给任何人。
            </p>
            <n-table size="small" :bordered="false" style="margin-top: var(--s3)">
              <thead>
                <tr>
                  <th>生效日</th>
                  <th>商品</th>
                  <th>人</th>
                  <th class="right">费率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in preview.preview || []" :key="i">
                  <td class="xs num">{{ r.effective_from }}</td>
                  <td class="xs">{{ r.product_name || r.product_id || '（店铺兜底）' }}</td>
                  <td>{{ r.person }}</td>
                  <td class="right num">{{ percent(Number(r.share)) }}</td>
                </tr>
              </tbody>
            </n-table>
          </div>
        </template>
      </div>
    </template>
  </n-spin>
</template>
