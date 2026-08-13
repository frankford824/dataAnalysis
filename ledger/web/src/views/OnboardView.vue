<script setup>
/* 接一张系统没见过的表。
 *
 * 这件事是一次性的：接完之后没人会再回来看这张映射。而错的映射不报错，只是静默
 * 少算钱。所以每一列旁边都要摆着「为什么这么提」和样例值，逼人当场核对——只给一个
 * 下拉框，人会一路点确定。
 */
import { useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import { count } from '../format'
import { useApp } from '../store'

const props = defineProps({ sha: { type: String, required: true } })

const app = useApp()
const route = useRoute()
const router = useRouter()
const message = useMessage()

const draft = ref(null)
const roleList = ref([])
const loading = ref(false)
const failed = ref('')
const assist = ref(null)
const tried = ref(null)

//: 人改过的列。序号 → 角色。模型的建议回来时不能盖掉这些。
const picked = ref({})
const templateId = ref('')
const source = ref('')
const headerRow = ref(null)

const CONFIDENCE = {
  sure: { label: '有把握', type: 'success' },
  ask: { label: '要你定', type: 'warning' },
  guess: { label: '按字面猜', type: 'default' },
  unknown: { label: '没见过', type: 'error' },
}

const columns = computed(() => draft.value?.columns || [])
const needsYou = computed(() => columns.value.filter((c) => !c.settled))
const rest = computed(() => columns.value.filter((c) => c.settled))

function roleOf(col) {
  if (col.index in picked.value) return picked.value[col.index]
  return col.role || ''
}

async function load() {
  loading.value = true
  failed.value = ''
  tried.value = null
  try {
    const params = { sheet: route.query.sheet, header_row: headerRow.value, source: source.value }
    draft.value = await api.draft(props.sha, params)
    templateId.value = templateId.value || draft.value.suggest_id
    source.value = source.value || draft.value.source
    headerRow.value = headerRow.value ?? draft.value.header_row
    roleList.value = (await api.roles(source.value)).roles || []
    // 模型建议慢，单独走一趟，失败就当没有——它只是提议，不接也能配。
    api
      .assist(props.sha, params)
      .then((res) => {
        assist.value = res.assist
        if (res.columns) mergeAssist(res.columns)
      })
      .catch(() => {})
  } catch (e) {
    failed.value = e.message
  } finally {
    loading.value = false
  }
}

/** 模型回来的映射只填人没动过的列。 */
function mergeAssist(cols) {
  if (!draft.value) return
  const mine = picked.value
  draft.value.columns = draft.value.columns.map((c) => {
    const fresh = cols.find((x) => x.index === c.index)
    return fresh && !(c.index in mine) ? fresh : c
  })
}

watch(() => props.sha, load, { immediate: true })

function commit() {
  const roles = {}
  for (const c of columns.value) roles[c.index] = roleOf(c)
  return {
    sha: props.sha,
    sheet: draft.value.sheet || '',
    header_row: headerRow.value,
    template_id: templateId.value,
    source: source.value,
    roles,
    match_columns: draft.value.match_columns || [],
    time_slots: draft.value.time_slots || {},
    total_row_marker: draft.value.total_row_marker || null,
    model_revision: draft.value.model_revision,
  }
}

async function tryIt() {
  try {
    tried.value = await app.run('正在试跑', () => api.onboardTry(commit()))
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

async function save() {
  try {
    const res = await app.run('正在落库并重算', () => api.onboard(commit()))
    app.invalidate()
    message.success(`接上了：${res.template_id}`)
    router.push('/')
  } catch (e) {
    message.error(`没落库：${e.message}`, { duration: 6000 })
  }
}

function change(col, role) {
  picked.value = { ...picked.value, [col.index]: role }
  // 改了映射，之前那次试跑就不算数了。留着会让人照着旧结果点落库。
  tried.value = null
}

const roleOptions = computed(() => [
  { label: '（这列不要）', value: '' },
  ...roleList.value.map((r) => ({ label: `${r.name || r.role}（${r.role}）`, value: r.role })),
])
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="failed" type="error" :bordered="false">{{ failed }}</n-alert>

    <template v-else-if="draft">
      <h1>接一张新表</h1>
      <div class="small muted" style="margin-bottom: var(--s4)">
        {{ draft.file }}
        <template v-if="draft.sheet"> · {{ draft.sheet }}</template>
        · {{ count(draft.rows) }} 行
      </div>

      <n-alert
        v-for="(w, i) in draft.warnings || []"
        :key="i"
        type="warning"
        :bordered="false"
        style="margin-bottom: var(--s2)"
      >
        {{ w }}
      </n-alert>

      <div class="card">
        <header><h2>这张表是什么</h2></header>
        <n-space vertical>
          <n-input-number v-model:value="headerRow" size="small" :min="0" @update:value="load">
            <template #prefix>表头在第</template>
            <template #suffix>行</template>
          </n-input-number>
          <n-select
            v-model:value="source"
            size="small"
            :options="(draft.sources || []).map((s) => ({ label: s.name, value: s.id }))"
            placeholder="这张表属于哪个数据源"
            style="max-width: 320px"
            @update:value="load"
          />
          <n-input v-model:value="templateId" size="small" placeholder="模板 id" style="max-width: 320px" />
        </n-space>
      </div>

      <div v-if="assist" class="card">
        <header>
          <h2>模型看过了</h2>
          <span class="sub">{{ assist.model }} · {{ assist.elapsed_ms }}ms</span>
        </header>
        <p class="small">{{ assist.summary }}</p>
      </div>

      <div class="card">
        <header>
          <h2>要你拍板的 {{ needsYou.length }} 列</h2>
          <span class="sub">错的映射不报错，只是静默少算钱</span>
        </header>
        <n-table size="small" :bordered="false">
          <thead>
            <tr>
              <th>列名</th>
              <th>样例</th>
              <th>为什么这么提</th>
              <th style="width: 220px">当成什么</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in needsYou" :key="c.index">
              <td>
                {{ c.column }}
                <n-tag size="tiny" :type="CONFIDENCE[c.confidence]?.type || 'default'" :bordered="false">
                  {{ CONFIDENCE[c.confidence]?.label || c.confidence }}
                </n-tag>
              </td>
              <td class="xs muted num">{{ (c.samples || []).slice(0, 2).join(' / ') }}</td>
              <td class="xs muted">{{ c.model_why || c.why }}</td>
              <td>
                <n-select
                  :value="roleOf(c)"
                  size="small"
                  filterable
                  :options="roleOptions"
                  @update:value="(v) => change(c, v)"
                />
              </td>
            </tr>
          </tbody>
        </n-table>
        <p v-if="!needsYou.length" class="small muted">没有要拍板的列。</p>

        <div class="panel">
          <h3>另外 {{ rest.length }} 列</h3>
          <n-table size="small" :bordered="false">
            <tbody>
              <tr v-for="c in rest" :key="c.index">
                <td>{{ c.column }}</td>
                <td class="xs muted">{{ c.why }}</td>
                <td style="width: 220px">
                  <n-select
                    :value="roleOf(c)"
                    size="small"
                    filterable
                    :options="roleOptions"
                    @update:value="(v) => change(c, v)"
                  />
                </td>
              </tr>
            </tbody>
          </n-table>
        </div>
      </div>

      <div class="card">
        <header>
          <h2>试跑</h2>
          <span class="sub">先拿这张表真跑一遍，看得出数才落库</span>
        </header>
        <div class="row">
          <n-button size="small" @click="tryIt">试跑</n-button>
          <n-button size="small" type="primary" :disabled="!tried?.ok" @click="save">
            落库并重算
          </n-button>
        </div>

        <template v-if="tried">
          <n-alert
            :type="tried.ok ? 'success' : 'error'"
            :bordered="false"
            style="margin-top: var(--s3)"
          >
            {{ tried.summary }}
          </n-alert>
          <n-table v-if="tried.roles?.length" size="small" :bordered="false" style="margin-top: var(--s3)">
            <thead>
              <tr>
                <th>角色</th>
                <th>取自哪列</th>
                <th class="right">有值的行</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in tried.roles" :key="r.role">
                <td>{{ r.role }}</td>
                <td class="xs muted">{{ r.column }}</td>
                <td class="right num">{{ count(r.filled) }}</td>
              </tr>
            </tbody>
          </n-table>
        </template>
      </div>
    </template>
  </n-spin>
</template>
