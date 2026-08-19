<script setup>
/* 费项台账。

 * 对账和引擎对不上，十有八九是某一条业务描述或备注没有挂到口径项上。
 * 这一页把「引擎认识什么 / 这个月认不出什么 / 人配了哪些规则」摊在同一处，
 * 配完先试算一家店看损益哪几行会变，确认后再落库、重算全部有表的店。
 *
 * 模型只给建议，不落库。exclude 和「没挂上订单也进账」两个开关能静默改利润，
 * 必须人自己勾，试算里会单独标出来。
 */
import { useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { api } from '../api'
import { count, money, prettyUnmatched, stamp } from '../format'
import { useApp } from '../store'

const app = useApp()
const route = useRoute()
const message = useMessage()

const data = ref(null)
const loading = ref(false)
const failed = ref('')
const tab = app.noted('fees.tab', 'unmatched')
const draft = ref([])
const editing = ref(null)
const editIndex = ref(-1)
const preview = ref(null)
const suggesting = ref(false)
const picked = ref({})

const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(data.value?.rules || []))

// 引擎已认识的规则按平台切。237 条混在一张表里，要找淘宝的得先滚过 1688。
const knownPlat = app.noted('fees.knownPlat', '')

const knownByPlatform = computed(() => {
  const rows = data.value?.known || []
  const plats = data.value?.platforms || []
  const buckets = new Map()
  for (const f of rows) {
    const id = f.platform || '*'
    if (!buckets.has(id)) buckets.set(id, [])
    buckets.get(id).push(f)
  }
  const tabs = []
  for (const p of plats) {
    const list = buckets.get(p.id)
    if (list?.length) tabs.push({ id: p.id, name: p.name, rows: list })
  }
  const shared = buckets.get('*')
  if (shared?.length) tabs.push({ id: '*', name: '各平台共用', rows: shared })
  const seen = new Set(tabs.map((t) => t.id))
  for (const [id, list] of buckets) {
    if (!seen.has(id) && list.length) {
      tabs.push({ id, name: platformName(id), rows: list })
    }
  }
  return tabs
})

const knownPlatShown = computed({
  get() {
    const tabs = knownByPlatform.value
    const want = knownPlat.value || app.platform
    if (want && tabs.some((t) => t.id === want)) return want
    return tabs[0]?.id || ''
  },
  set(v) {
    knownPlat.value = v
  },
})

watch(
  () => app.platform,
  (id) => {
    if (id && knownByPlatform.value.some((t) => t.id === id)) knownPlat.value = id
  },
)

const fieldOptions = computed(() => (data.value?.fields || []).map((f) => ({ label: f.name, value: f.id })))
const howOptions = computed(() => (data.value?.hows || []).map((f) => ({ label: f.name, value: f.id })))
const stageOptions = computed(() => (data.value?.stages || []).map((f) => ({ label: f.name, value: f.id })))
const majorOptions = computed(() => (data.value?.majors || []).map((m) => ({ label: m.name, value: m.id })))
const platformOptions = computed(() => [
  { label: '全部平台', value: '*' },
  ...(data.value?.platforms || []).map((p) => ({ label: p.name, value: p.id })),
])

function majorName(id) {
  return (data.value?.majors || []).find((m) => m.id === id)?.name || id || '—'
}
function fieldName(id) {
  return (data.value?.fields || []).find((f) => f.id === id)?.name || id
}
function howName(id) {
  return (data.value?.hows || []).find((f) => f.id === id)?.name || id
}
function platformName(id) {
  if (!id || id === '*') return '全部平台'
  return (
    (data.value?.platforms || []).find((p) => p.id === id)?.name
    || (data.value?.platform_aliases || {})[id]
    || id
  )
}
function originName(row) {
  return row.origin_name || (row.origin === 'dictionary' ? '科目字典' : row.origin)
}
function unmatchedCaption(row) {
  return row.caption || prettyUnmatched(row.label)
}
function unmatchedKey(row) {
  return `${row.field || ''}|${row.how || ''}|${row.value || row.label || ''}`
}

const pickedRows = computed(() =>
  (data.value?.unmatched || []).filter((u) => picked.value[unmatchedKey(u)]),
)
const allPicked = computed(() => {
  const rows = data.value?.unmatched || []
  return rows.length > 0 && rows.every((u) => picked.value[unmatchedKey(u)])
})

function togglePick(row, on) {
  picked.value = { ...picked.value, [unmatchedKey(row)]: on }
}
function toggleAll(on) {
  if (!on) {
    picked.value = {}
    return
  }
  const next = {}
  for (const u of data.value?.unmatched || []) next[unmatchedKey(u)] = true
  picked.value = next
}

function addBatch() {
  const rows = pickedRows.value
  if (!rows.length) {
    message.warning('先勾要一起归类的条目')
    return
  }
  const values = [...new Set(rows.map((r) => (r.value || '').trim()).filter(Boolean))]
  const shortest = [...values].sort((a, b) => a.length - b.length)[0] || ''
  const shared = values.length && values.every((v) => v.includes(shortest)) ? shortest : shortest
  const fields = [...new Set(rows.map((r) => r.field).filter(Boolean))]
  addFrom({
    ...rows[0],
    field: fields.length === 1 ? fields[0] : 'remark',
    how: 'contains',
    value: shared,
    caption: `已选 ${rows.length} 类`,
    label: rows[0].label,
  })
}

const opened = ref('')

async function load() {
  loading.value = true
  failed.value = ''
  try {
    data.value = await api.fees()
    draft.value = JSON.parse(JSON.stringify(data.value.rules || []))
    const label = route.query.label
    if (label && typeof label === 'string' && label !== opened.value) {
      opened.value = label
      const hit = (data.value.unmatched || []).find((u) => u.label === label)
      if (hit) addFrom(hit)
      else addFrom({ label, field: 'subject', value: label, how: 'exact' })
    }
  } catch (e) {
    failed.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => route.query.label, load, { immediate: true })

function blank() {
  return {
    platform: app.platform || '*',
    field: 'subject',
    how: 'exact',
    value: '',
    major: '',
    minor: '',
    exclude: false,
    count_without_order: false,
    stage: 'after',
    note: '',
    by: '',
    at: '',
  }
}

function addFrom(item) {
  editing.value = {
    ...blank(),
    platform: item.platforms?.[0] || app.platform || '*',
    field: item.field || 'subject',
    how: item.how || 'exact',
    value: item.value || item.label || '',
    note: item.label && item.value !== item.label
      ? `来自未归类：${item.caption || prettyUnmatched(item.label)}`
      : '',
  }
  editIndex.value = -1
  tab.value = 'rules'
}

function editAt(i) {
  editing.value = { ...blank(), ...draft.value[i] }
  editIndex.value = i
}

function saveEdit() {
  const row = editing.value
  if (!row.value?.trim()) {
    message.error('匹配值是空的')
    return
  }
  if (!row.exclude && !row.major) {
    message.error('要么选费项，要么标成排除')
    return
  }
  const next = { ...row, value: row.value.trim(), minor: (row.minor || '').trim() }
  if (editIndex.value < 0) draft.value = [...draft.value, next]
  else draft.value.splice(editIndex.value, 1, next)
  editing.value = null
  picked.value = {}
}

function dropAt(i) {
  draft.value = draft.value.filter((_, j) => j !== i)
}

function move(i, dir) {
  const j = i + dir
  if (j < 0 || j >= draft.value.length) return
  const copy = [...draft.value]
  const [row] = copy.splice(i, 1)
  copy.splice(j, 0, row)
  draft.value = copy
}

async function suggest() {
  if (!editing.value?.value) return
  suggesting.value = true
  try {
    const got = await api.feesSuggest({ label: editing.value.value, field: editing.value.field })
    if (!got.ok) {
      message.warning(got.note || '没有建议')
      return
    }
    if (got.major) editing.value.major = got.major
    if (got.minor && !editing.value.minor) editing.value.minor = got.minor
    if (got.exclude) editing.value.exclude = true
    message.info(got.why || got.note)
  } catch (e) {
    message.error(e.message)
  } finally {
    suggesting.value = false
  }
}

async function runPreview() {
  if (!app.storeId) {
    message.warning('先在顶上选一家店，试算只跑这一家')
    return
  }
  try {
    preview.value = await app.run('正在试算这家店', () =>
      api.feesPreview({ rules: draft.value, store_id: app.storeId }),
    )
    tab.value = 'rules'
  } catch (e) {
    message.error(e.message)
  }
}

async function apply() {
  if (!preview.value) {
    message.warning('先试算一家店，看损益哪几行会变，再落库')
    return
  }
  try {
    await app.run('正在落库并重算', () =>
      api.feesApply({
        rules: draft.value,
        store_id: app.storeId,
        recompute: true,
        note: `界面改费项规则，共 ${draft.value.length} 条`,
      }),
    )
    preview.value = null
    app.invalidate()
    await load()
    message.success('规则已生效，有表的店都重算过了')
  } catch (e) {
    message.error(e.message)
  }
}
</script>

<template>
  <n-alert v-if="failed" type="error" :bordered="false" style="margin-bottom: var(--s4)">
    {{ failed }}
  </n-alert>

  <div class="spread" style="margin-bottom: var(--s4)">
    <div>
      <h1>费项</h1>
      <div class="small muted">
        把到账里的业务描述、备注归到对应费项。尚未归属的新费项会在这里列出来。
      </div>
    </div>
    <div class="row">
      <n-button size="small" :disabled="!dirty" @click="runPreview">试算当前店</n-button>
      <n-button size="small" type="primary" :disabled="!dirty || !preview" @click="apply">
        落库并重算
      </n-button>
    </div>
  </div>

  <n-spin :show="loading">
    <div class="card">
      <n-tabs v-model:value="tab" type="line" size="small">
        <n-tab-pane name="unmatched">
          <template #tab>
            未归类
            <n-badge
              v-if="data?.unmatched?.length"
              :value="data.unmatched.length"
              type="warning"
              style="margin-left: 6px"
            />
          </template>
          <p class="xs muted" style="margin-bottom: var(--s3); line-height: 1.55; white-space: normal">
            以下流水未能归入任何费项，因此未计入本期损益。勾选相近的条目可一次归到同一个费项。
          </p>
          <n-empty v-if="!data?.unmatched?.length" description="目前没有未归类的流水" />
          <template v-else>
            <div class="row" style="margin-bottom: var(--s3)">
              <n-checkbox :checked="allPicked" @update:checked="toggleAll">全选</n-checkbox>
              <n-button size="small" :disabled="!pickedRows.length" @click="addBatch">
                批量归到费项{{ pickedRows.length ? `（${pickedRows.length}）` : '' }}
              </n-button>
            </div>
            <div class="scroll">
              <n-table size="small" :bordered="false" :single-line="false">
                <thead>
                  <tr>
                    <th style="width: 36px" />
                    <th>业务描述 / 备注</th>
                    <th>匹配方式</th>
                    <th class="right">笔数</th>
                    <th class="right">金额</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(u, i) in data.unmatched" :key="i">
                    <td>
                      <n-checkbox
                        :checked="!!picked[unmatchedKey(u)]"
                        @update:checked="(v) => togglePick(u, v)"
                      />
                    </td>
                    <td class="small wrap-cell">{{ unmatchedCaption(u) }}</td>
                    <td class="xs muted nowrap">{{ fieldName(u.field) }} · {{ howName(u.how) }}</td>
                    <td class="right num xs">{{ count(u.count) }}</td>
                    <td class="right num" :class="{ neg: u.amount < 0 }">{{ money(u.amount) }}</td>
                    <td class="nowrap">
                      <button class="link" type="button" @click="addFrom(u)">归到费项 →</button>
                    </td>
                  </tr>
                </tbody>
              </n-table>
            </div>
          </template>
        </n-tab-pane>

        <n-tab-pane name="rules" :tab="`已配规则（${draft.length}）`">
          <p class="xs muted" style="margin-bottom: var(--s3)">
            同一档内，排在上面的规则优先。新增规则默认只处理尚未归类的流水；
            选「优先于现有规则」会改写模板里已有的归类，损益金额可能变化，请先试算。
          </p>
          <div class="row" style="margin-bottom: var(--s3)">
            <n-button size="small" @click="editing = blank(); editIndex = -1">
              新增规则
            </n-button>
            <span v-if="dirty" class="xs muted">尚未保存</span>
          </div>
          <n-empty v-if="!draft.length" description="还没有从这里添加过规则" />
          <div v-else class="scroll">
            <n-table size="small" :bordered="false" :single-line="false">
              <thead>
                <tr>
                  <th>档</th>
                  <th>平台</th>
                  <th>匹配</th>
                  <th>归到</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in draft" :key="i">
                  <td class="xs">{{ r.stage === 'before' ? '优先执行' : '尚未归类' }}</td>
                  <td class="xs">{{ platformName(r.platform) }}</td>
                  <td class="small">
                    {{ fieldName(r.field) }} {{ howName(r.how) }}
                    <b>{{ r.value }}</b>
                    <div v-if="r.exclude" class="xs muted">命中后排除，不计入损益</div>
                    <div v-else-if="r.count_without_order" class="xs muted">未关联订单也计入损益</div>
                  </td>
                  <td class="small">
                    {{ r.exclude ? '排除' : majorName(r.major) }}
                    <div v-if="r.minor" class="xs muted">{{ r.minor }}</div>
                  </td>
                  <td class="row">
                    <button class="link" type="button" @click="move(i, -1)">上移</button>
                    <button class="link" type="button" @click="move(i, 1)">下移</button>
                    <button class="link" type="button" @click="editAt(i)">改</button>
                    <button class="link" type="button" @click="dropAt(i)">删</button>
                  </td>
                </tr>
              </tbody>
            </n-table>
          </div>

          <div v-if="preview" style="margin-top: var(--s5)">
            <h3>试算 · {{ preview.store }}</h3>
            <p class="xs muted" style="margin-bottom: var(--s3)">
              还没落库。下面是这家店每个账期损益表上会变的行。
            </p>
            <div v-for="p in preview.periods" :key="p.period" style="margin-bottom: var(--s4)">
              <div class="small strong">{{ p.period }}</div>
              <div class="xs muted">
                尚未归类 {{ p.unclassified_before }} → {{ p.unclassified_after }}
              </div>
              <n-empty v-if="!p.diff?.length" description="损益数字没有变化" />
              <n-table v-else size="small" :bordered="false" :single-line="false">
                <thead>
                  <tr>
                    <th>项目</th>
                    <th class="right">现在</th>
                    <th class="right">试算后</th>
                    <th class="right">差</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="d in p.diff" :key="d.id">
                    <td class="small">{{ d.name }}</td>
                    <td class="right num xs">{{ money(d.before) }}</td>
                    <td class="right num xs">{{ money(d.after) }}</td>
                    <td class="right num" :class="{ neg: d.delta < 0 }">{{ money(d.delta) }}</td>
                  </tr>
                </tbody>
              </n-table>
            </div>
          </div>
        </n-tab-pane>

        <n-tab-pane name="known" :tab="`已有归类（${data?.known?.length || 0}）`">
          <p class="xs muted" style="margin-bottom: var(--s3)">
            科目字典的精确匹配，以及各对账模板里写好的规则。要改这些需要发版；新费项请在「已配规则」里添加。
          </p>
          <n-tabs
            v-model:value="knownPlatShown"
            type="segment"
            size="small"
            style="margin-bottom: var(--s3)"
          >
            <n-tab-pane
              v-for="g in knownByPlatform"
              :key="g.id"
              :name="g.id"
              :tab="`${g.name}（${g.rows.length}）`"
            >
              <div class="scroll">
                <n-table size="small" :bordered="false" :single-line="false">
                  <thead>
                    <tr>
                      <th>匹配</th>
                      <th>归到</th>
                      <th>来源</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(f, i) in g.rows" :key="i">
                      <td class="small">
                        {{ fieldName(f.field) }} {{ howName(f.how) }}
                        <b>{{ f.key }}</b>
                      </td>
                      <td class="small">{{ f.excluded ? '排除' : majorName(f.major) }}</td>
                      <td class="xs muted">
                        {{ originName(f) }}
                      </td>
                    </tr>
                  </tbody>
                </n-table>
              </div>
            </n-tab-pane>
          </n-tabs>
        </n-tab-pane>

        <n-tab-pane name="log" tab="改动记录">
          <n-empty v-if="!data?.log?.length" description="还没有从界面改过费项规则" />
          <ul v-else class="bullets">
            <li v-for="row in data.log" :key="row.id">
              {{ stamp(row.at) }} · {{ row.by || '—' }} · {{ row.summary }}
            </li>
          </ul>
        </n-tab-pane>
      </n-tabs>
    </div>
  </n-spin>

  <n-modal
    :show="!!editing"
    preset="card"
    :title="editIndex < 0 ? '新增费项规则' : '修改这条规则'"
    style="max-width: 640px"
    @update:show="(v) => { if (!v) editing = null }"
  >
    <div v-if="editing" class="stack" style="gap: var(--s3)">
      <n-select v-model:value="editing.platform" :options="platformOptions" size="small" />
      <div class="row" style="align-items: stretch">
        <n-select
          v-model:value="editing.field"
          :options="fieldOptions"
          size="small"
          style="flex: 1; min-width: 140px"
          :consistent-menu-width="false"
        />
        <n-select
          v-model:value="editing.how"
          :options="howOptions"
          size="small"
          style="flex: 1; min-width: 180px"
          :consistent-menu-width="false"
        />
      </div>
      <n-input v-model:value="editing.value" size="small" placeholder="业务描述或备注里的词" />
      <n-select
        v-model:value="editing.major"
        :options="majorOptions"
        size="small"
        filterable
        clearable
        placeholder="归到哪个费项"
        :disabled="editing.exclude"
      />
      <n-input v-model:value="editing.minor" size="small" placeholder="细项（对账表上显示的名字，可空）" />
      <n-select
        v-model:value="editing.stage"
        :options="stageOptions"
        size="small"
        :consistent-menu-width="false"
      />
      <n-checkbox v-model:checked="editing.exclude">命中后排除，不计入损益</n-checkbox>
      <n-checkbox v-model:checked="editing.count_without_order" :disabled="editing.exclude">
        未关联本期订单也计入损益
      </n-checkbox>
      <p class="xs muted">
        排除或未关联订单计入，都会改变利润。勾选前请先试算。
      </p>
      <n-input v-model:value="editing.note" size="small" placeholder="备注（可空）" />
    </div>
    <template #footer>
      <div class="row" style="justify-content: space-between">
        <n-button size="small" :loading="suggesting" @click="suggest">请模型建议</n-button>
        <div class="row">
          <n-button size="small" @click="editing = null">取消</n-button>
          <n-button size="small" type="primary" @click="saveEdit">保存</n-button>
        </div>
      </div>
    </template>
  </n-modal>
</template>
