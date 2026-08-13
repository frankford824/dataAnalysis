<script setup>
/* 一家店一个账期：损益表、自检、该交的表、质量、结账。
 *
 * 损益表每一行都能点开——这是这套系统和一张普通报表的唯一区别。数字点不开，
 * 对不上账时人就只能回去用 Excel 手工核。
 */
import { useDialog, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import DrillDrawer from '../components/DrillDrawer.vue'
import DropZone from '../components/DropZone.vue'
import { count, money, percent, stamp } from '../format'
import { useApp } from '../store'

const props = defineProps({ id: { type: String, required: true } })

const app = useApp()
const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const info = ref(null)
const snap = ref(null)
const loading = ref(false)
const failed = ref('')
const drill = ref(null)

const period = computed(() => route.query.period || app.period || '')

// 接口只给平台 id。人看的是「淘宝天猫」，不是 taobao。
const platformName = computed(() => {
  const id = info.value?.store?.platform
  return app.platforms.find((p) => p.id === id)?.name || id || ''
})

const closed = computed(() => snap.value?.state === 'closed')

async function load() {
  loading.value = true
  failed.value = ''
  try {
    info.value = await api.store(props.id)
    const want = period.value || info.value.periods?.[0]?.period
    if (want) {
      snap.value = await api.period(props.id, want)
      app.pick({ store: props.id, period: want })
    } else {
      snap.value = null
    }
  } catch (e) {
    failed.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => [props.id, period.value], load, { immediate: true })

function go(p) {
  router.replace({ name: 'period', params: { id: props.id }, query: { period: p } })
}

async function recompute() {
  try {
    await app.run('正在重算', () => api.recompute(props.id))
    app.invalidate()
    await load()
    message.success('算完了')
  } catch (e) {
    message.error(`没算成：${e.message}`, { duration: 6000 })
  }
}

async function close() {
  try {
    await app.run('正在结账', () => api.close(props.id, period.value))
    app.invalidate()
    await load()
    message.success('结账了')
  } catch (e) {
    message.error(`结不了：${e.message}`, { duration: 6000 })
  }
}

const why = ref('')
const asking = ref(false)

/** 反结账要写理由。账已经报出去了，改回去必须留下是谁、为什么。 */
async function reopen() {
  if (!why.value.trim()) return
  try {
    await app.run('正在反结账', () => api.reopen(props.id, period.value, why.value.trim()))
    asking.value = false
    why.value = ''
    app.invalidate()
    await load()
    message.success('已改回未结账')
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

function openDrill(row) {
  if (!row.drillable || !snap.value?.run_id) return
  drill.value = { runId: snap.value.run_id, node: row.id, name: row.name, value: row.value }
}
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="failed" type="error" :bordered="false">{{ failed }}</n-alert>

    <template v-else-if="info">
      <div class="spread" style="margin-bottom: var(--s4)">
        <div>
          <h1>{{ info.store?.name }}</h1>
          <div class="small muted">
            {{ platformName }}
            <template v-if="info.store?.entity"> · {{ info.store.entity }}</template>
            · 已收 {{ count(info.files?.length || 0) }} 张表
          </div>
        </div>
        <n-space>
          <n-button size="small" @click="recompute">重算</n-button>
          <n-button v-if="!closed" size="small" type="primary" :disabled="!snap?.can_close" @click="close">
            结账
          </n-button>
          <n-button v-else size="small" @click="asking = true">反结账</n-button>
        </n-space>
      </div>

      <n-space style="margin-bottom: var(--s4)">
        <n-button
          v-for="p in info.periods || []"
          :key="p.period"
          size="small"
          :type="p.period === period ? 'primary' : 'default'"
          @click="go(p.period)"
        >
          {{ p.period }}
          <span v-if="p.state === 'closed'" class="xs">· 已结</span>
          <span v-else-if="p.can_close === false" class="xs">· 结不了</span>
        </n-button>
      </n-space>

      <template v-if="snap">
        <n-alert
          v-if="closed"
          type="success"
          :bordered="false"
          style="margin-bottom: var(--s4)"
        >
          已结账{{ snap.at ? `于 ${stamp(snap.at)}` : '' }}{{ snap.by ? ` · ${snap.by}` : '' }}
          <template v-if="snap.stale"> · 之后又交了新表，数字还是结账那一版</template>
        </n-alert>

        <div class="card">
          <header>
            <h2>损益表</h2>
            <span class="sub">点任意一行看它是怎么来的</span>
          </header>
          <div class="statement">
            <div
              v-for="row in snap.statement || []"
              :key="row.id"
              class="line"
              :class="[`lv${row.level}`, { total: row.is_total, drillable: row.drillable }]"
              @click="openDrill(row)"
            >
              <span>{{ row.name }}</span>
              <span v-if="!row.available" class="na">—</span>
              <span v-else class="amt" :class="{ neg: row.value < 0 }">
                {{ row.display === 'percent' ? percent(row.value) : money(row.value) }}
              </span>
            </div>
          </div>
          <p v-if="(snap.missing_sources || []).length" class="why" style="margin-top: var(--s3)">
            破折号的行是还不知道，不是零。缺：{{ snap.missing_sources.join('、') }}
          </p>
        </div>

        <div class="card">
          <header><h2>自检</h2></header>
          <n-alert
            v-for="f in (snap.findings || []).filter((x) => !x.passed)"
            :key="f.id"
            :type="f.blocking ? 'error' : 'warning'"
            :bordered="false"
            style="margin-bottom: var(--s2)"
          >
            {{ f.message }}
          </n-alert>
          <n-alert
            v-if="!(snap.findings || []).some((x) => !x.passed)"
            type="success"
            :bordered="false"
          >
            {{ (snap.findings || []).length }} 项检查都过了
          </n-alert>
        </div>

        <div class="card">
          <header>
            <h2>该交的表</h2>
            <span class="sub">缺一张，损益表上就有一行出不了数</span>
          </header>
          <n-table size="small" :bordered="false">
            <tbody>
              <tr v-for="s in snap.sources || []" :key="s.id">
                <td>{{ s.name }}</td>
                <td class="right">
                  <n-tag size="small" :type="s.arrived ? 'success' : 'warning'" :bordered="false">
                    {{ s.arrived ? '已交' : s.reason || '没交' }}
                  </n-tag>
                </td>
              </tr>
            </tbody>
          </n-table>
        </div>

        <div v-if="snap.quality?.length" class="card">
          <header>
            <h2>数据质量</h2>
            <span class="sub">挂钩率是这张表有多少行认到了订单，覆盖率是订单里有多少拿到了这项数</span>
          </header>
          <n-table size="small" :bordered="false">
            <thead>
              <tr>
                <th>项目</th>
                <th class="right">行数</th>
                <th class="right">挂钩率</th>
                <th class="right">覆盖率</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="q in snap.quality" :key="q.metric">
                <td>
                  {{ q.name }}
                  <span v-if="q.company_wide" class="xs muted">· 全公司表</span>
                </td>
                <td class="right num">{{ count(q.rows) }}</td>
                <td class="right num">{{ q.hit_rate === null ? '—' : percent(q.hit_rate) }}</td>
                <td class="right num">
                  {{ q.coverage === null ? '—' : percent(q.coverage) }}
                  <span v-if="q.expect_label" class="xs muted">（{{ q.expect_label }}）</span>
                </td>
              </tr>
            </tbody>
          </n-table>
        </div>

        <div v-if="snap.unlinked_total" class="card">
          <header>
            <h2>没进利润的钱</h2>
            <span class="sub">合计 <span class="num">{{ money(snap.unlinked_total) }}</span></span>
          </header>
          <n-table size="small" :bordered="false">
            <tbody>
              <tr v-for="(b, i) in snap.unlinked_buckets || []" :key="i">
                <td>{{ b.name || b.bucket }}<div class="xs muted">{{ b.why }}</div></td>
                <td class="right num">{{ money(b.amount) }}</td>
              </tr>
            </tbody>
          </n-table>
        </div>
      </template>

      <n-empty v-else description="这家店还没有算出来的账期" style="padding: var(--s7) 0">
        <template #extra><DropZone /></template>
      </n-empty>

      <DropZone style="margin-top: var(--s4)" />
    </template>

    <n-modal
      v-model:show="asking"
      preset="dialog"
      title="反结账"
      positive-text="确定"
      negative-text="算了"
      :positive-button-props="{ disabled: !why.trim() }"
      @positive-click="reopen"
    >
      <p class="small muted" style="margin-bottom: var(--s3)">
        这个账期已经报出去了。写清为什么要改回去——这条会记在账期历史里。
      </p>
      <n-input v-model:value="why" type="textarea" :rows="3" placeholder="比如：运费表交漏了一张，补传后重算" />
    </n-modal>

    <DrillDrawer
      v-if="drill"
      :run-id="drill.runId"
      :node="drill.node"
      :title="drill.name"
      @close="drill = null"
    />
  </n-spin>
</template>
