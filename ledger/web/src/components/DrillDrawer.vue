<script setup>
/* 一个报表数字是怎么来的。
 *
 * 三层：先看这个数由哪些科目、哪些文件堆出来，再看具体行，每行带文件名、
 * 工作表、行号。人是拿着行号回源文件核对的，所以行号不能省，也不能指错。
 *
 * 默认只给进了账的行，它们加起来正好等于报表上那个数。源表里还有一批挂不上
 * 这家店订单的行——运费表是全公司的运单，淘宝那家店 29.9 万行里只有 1.4 万行
 * 是自己的——那部分单独一栏，想看再点。全摆在一起的话，下钻显示 -550,944 而
 * 报表写着 -20,294，人只会认为报表算错了。
 */
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import { count, money } from '../format'

const props = defineProps({
  runId: { type: Number, required: true },
  node: { type: String, required: true },
  title: { type: String, default: '' },
  only: { type: String, default: 'counted' },
})
const emit = defineEmits(['close'])

const show = ref(true)
const data = ref(null)
const loading = ref(false)
const failed = ref('')

const page = ref(0)
const size = 100
const only = ref(props.only)
const subject = ref('')
const file = ref('')
const term = ref('')
const order = ref('amount')

const diff = computed(() => {
  const d = data.value
  if (!d || d.value === null || d.value === undefined) return null
  return d.source_total - d.value
})

async function load() {
  loading.value = true
  failed.value = ''
  try {
    data.value = await api.drill(props.runId, props.node, {
      limit: size,
      offset: page.value * size,
      only: only.value,
      subject: subject.value,
      file: file.value,
      q: term.value.trim(),
      order: order.value,
    })
  } catch (e) {
    failed.value = e.message
  } finally {
    loading.value = false
  }
}

watch([() => props.node, () => props.only], () => {
  only.value = props.only
  page.value = 0
  subject.value = ''
  file.value = ''
})

watch([() => props.node, only, subject, file, order, page], load, { immediate: true })

// 换筛选就回第一页。留在第 7 页上看一个只有 3 页的结果，界面会显示空白。
watch([only, subject, file, term], () => (page.value = 0))

function pickSubject(s) {
  subject.value = subject.value === s ? '' : s
}
function pickFile(f) {
  file.value = file.value === f ? '' : f
}
function close() {
  show.value = false
  emit('close')
}
</script>

<template>
  <n-drawer :show="show" :width="820" placement="right" @update:show="close">
    <n-drawer-content :title="title || node" closable>
      <n-spin :show="loading">
        <n-alert v-if="failed" type="error" :bordered="false">{{ failed }}</n-alert>

        <template v-else-if="data">
          <div class="board-kpis" style="grid-template-columns: repeat(2, 1fr)">
            <div class="kpi">
              <div class="label">
                {{ data.kind === 'statement' ? '报表上这个数' : '这些行的合计' }}
              </div>
              <div class="value" :class="{ neg: data.value < 0 }">{{ money(data.value) }}</div>
            </div>
            <div class="kpi">
              <div class="label">加起来</div>
              <div class="value" :class="{ neg: data.source_total < 0 }">
                {{ money(data.source_total) }}
              </div>
              <div class="foot">
                {{ count(data.rows) }} 行
                <template v-if="diff !== null && Math.abs(diff) > 0.005">
                  · 差 {{ money(diff) }}
                </template>
              </div>
            </div>
          </div>

          <n-alert
            v-if="!data.graded"
            type="warning"
            :bordered="false"
            style="margin-top: var(--s3)"
          >
            这次算账的留档还没有进账标记，下面是全部源记录，加起来对不上报表。重算一次就好了。
          </n-alert>

          <div class="row wrap" style="margin: var(--s4) 0; gap: var(--s2)">
            <n-radio-group v-if="data.kind === 'statement' || data.kind === 'metric'" v-model:value="only" size="small">
              <n-radio-button value="counted">进了账</n-radio-button>
              <n-radio-button value="uncounted">
                没进账
                <span v-if="data.uncounted?.rows" class="xs">
                  {{ count(data.uncounted.rows) }}
                </span>
              </n-radio-button>
              <n-radio-button value="all">全部</n-radio-button>
            </n-radio-group>
            <n-select
              v-model:value="order"
              size="small"
              style="width: 128px"
              :options="[
                { label: '金额大的在前', value: 'amount' },
                { label: '按源文件行号', value: 'row' },
              ]"
            />
            <n-input
              v-model:value="term"
              size="small"
              :placeholder="(data?.key_label || '订单号') + '或科目'"
              style="width: 200px"
              clearable
              @keyup.enter="page = 0; load()"
            />
            <n-button size="small" @click="page = 0; load()">筛</n-button>
          </div>

          <n-alert
            v-if="only === 'counted' && data.uncounted?.rows && data.kind === 'statement'"
            type="default"
            :bordered="false"
            style="margin-bottom: var(--s4)"
          >
            另有 {{ count(data.uncounted.rows) }} 行、合计
            <span class="num">{{ money(data.uncounted.amount) }}</span>
            没进这家店的账——多半是全公司的表里属于别家店铺的行。切到「没进账」能看。
          </n-alert>

          <n-table v-if="data.by_subject?.length" size="small" :bordered="false">
            <thead>
              <tr>
                <th>科目</th>
                <th class="right">行数</th>
                <th class="right">金额</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(s, i) in data.by_subject"
                :key="i"
                style="cursor: pointer"
                :class="{ quiet: subject && subject !== s.subject }"
                @click="pickSubject(s.subject)"
              >
                <td>{{ s.subject || '未分类' }}</td>
                <td class="right num">{{ count(s.count) }}</td>
                <td class="right num" :class="{ neg: s.amount < 0 }">{{ money(s.amount) }}</td>
              </tr>
            </tbody>
          </n-table>

          <h3 style="margin: var(--s5) 0 var(--s2)">来源文件</h3>
          <n-table size="small" :bordered="false">
            <tbody>
              <tr
                v-for="(f, i) in data.by_file || []"
                :key="i"
                style="cursor: pointer"
                :class="{ quiet: file && file !== f.file }"
                @click="pickFile(f.file)"
              >
                <td class="xs">{{ f.file }}<template v-if="f.sheet"> · {{ f.sheet }}</template></td>
                <td class="right num">{{ count(f.count) }}</td>
                <td class="right num" :class="{ neg: f.amount < 0 }">{{ money(f.amount) }}</td>
              </tr>
            </tbody>
          </n-table>

          <div class="spread" style="margin: var(--s5) 0 var(--s2)">
            <h3>原始行</h3>
            <span class="xs muted">
              第 {{ (data.selection?.offset || 0) + 1 }}–{{
                (data.selection?.offset || 0) + (data.sample?.length || 0)
              }}
              行，共 {{ count(data.selection?.rows || 0) }}
            </span>
          </div>
          <!-- 定位到行只解决了一半：人拿着行号翻到源文件，发现那儿确实不对，
               然后就卡住了——这套东西不给改单元格，而「那改哪儿」界面上没写过。 -->
          <p class="xs muted" style="margin-bottom: var(--s2)">
            最后一列是它在源文件里的位置，照着行号能翻回原表核对。
            核对下来确实不对的话，改的是表本身或者认表的口径，不是这里的数字——
            账期页右上角「数字不对？」写了怎么改。
          </p>
          <n-table size="small" :bordered="false">
            <thead>
              <tr>
                <th>{{ data.key_label || '订单号' }}</th>
                <th>科目</th>
                <th class="right">金额</th>
                <th class="right">进账</th>
                <th>在哪一行</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(r, i) in data.sample || []" :key="i">
                <td class="xs num">{{ r.link_key || '—' }}</td>
                <td class="xs">
                  {{ r.minor || r.subject || r.metric }}
                  <div v-if="r.classify_via" class="xs muted">{{ r.classify_via }}</div>
                </td>
                <td class="right num" :class="{ neg: r.amount < 0 }">{{ money(r.amount) }}</td>
                <td class="right num" :class="{ neg: r.contribution < 0 }">
                  {{ r.counted ? money(r.contribution) : '—' }}
                </td>
                <td class="xs num">
                  {{ r.file_name }}<template v-if="r.sheet"> · {{ r.sheet }}</template> ·
                  第 {{ r.row_no }} 行
                </td>
              </tr>
            </tbody>
          </n-table>

          <div class="row" style="margin-top: var(--s4); justify-content: center">
            <n-button size="small" :disabled="page === 0" @click="page -= 1">上一页</n-button>
            <span class="small muted num">{{ page + 1 }}</span>
            <n-button
              size="small"
              :disabled="!data.selection?.has_more"
              @click="page += 1"
            >
              下一页
            </n-button>
          </div>
        </template>
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>
