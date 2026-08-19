<script setup>
/* 一家店一个账期：损益表、自检、该交的表、质量、结账。
 *
 * 损益表每一行都能点开——这是这套系统和一张普通报表的唯一区别。数字点不开，
 * 对不上账时人就只能回去用 Excel 手工核。
 *
 * 排版上，损益表占主栏，剩下四块收进右边一栏的标签页里。它们回答的是同一个
 * 问题的另一半——「这张表能不能信」——所以必须和数字同屏；但它们又不是每次都
 * 要看，竖着铺开就把页面拉到三四屏长，人滚到底就忘了上面的数是多少。
 */
import { useDialog, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import DrillDrawer from '../components/DrillDrawer.vue'
import DropZone from '../components/DropZone.vue'
import FixPanel from '../components/FixPanel.vue'
import GapList from '../components/GapList.vue'
import PeriodStrip from '../components/PeriodStrip.vue'
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
  app.pick({ period: p })
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

const bad = computed(() => (snap.value?.findings || []).filter((f) => !f.passed))
//: 真正拦着结账的那几条。灰掉的结账按钮不说明理由，人只能猜是不是坏了。
const blockers = computed(() => bad.value.filter((f) => f.blocking))
const fixing = ref(false)
const missingSources = computed(() =>
  (snap.value?.sources || []).filter((s) => !s.arrived).length,
)

const gaps = computed(() => snap.value?.gaps || [])

/* 没进利润的钱。
 *
 * 要查的那一桶排最前，其余按金额。后端已经按这个顺序给了，这里只补一件事：
 * 让「算进合计的」和「不算进合计的」在视觉上分开——四行金额差几个数量级，
 * 不分开的话人第一眼会以为合计算错了。 */
const unlinkedRows = computed(() => snap.value?.unlinked_buckets || [])

/* 「哪几桶算进合计」是后来才加进快照的字段，早先算的快照里没有，而已结账的账期
 * 按设计就不会再重算——那份快照要一直保持结账那天的样子。所以这里不能默认「没写
 * 就是不算」：那样合计的笔数会显示成 0，看起来像刚数错了一遍。老快照就老老实实
 * 说不知道，金额照旧用快照里存着的那个总额。 */
const unlinkedGraded = computed(() =>
  unlinkedRows.value.some((b) => typeof b.counted === 'boolean'),
)

const unlinkedNeedsWork = computed(() => ({
  count: unlinkedRows.value
    .filter((b) => b.counted)
    .reduce((a, b) => a + (b.count || 0), 0),
}))

/** 点一条缺口，直接落到损益表上那一行的下钻。 */
function openGap(gap) {
  const row = (snap.value?.statement || []).find((r) => r.id === gap.node)
  if (row) openDrill(row)
}

// 右栏默认停在最需要人处理的那一块：有缺口就停在缺口，有没交的表就停在该交的表，
// 都没有才停在质量。默认永远停在第一个标签的话，真正要人处理的那条就藏在后面。
const rail = ref('gaps')
watch(
  () => snap.value,
  () => {
    rail.value = gaps.value.length ? 'gaps' : missingSources.value ? 'sources' : 'quality'
  },
)
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
          <n-button size="small" @click="fixing = true">数字不对？</n-button>
          <n-button size="small" @click="recompute">重算</n-button>
          <n-button v-if="!closed" size="small" type="primary" :disabled="!snap?.can_close" @click="close">
            结账
          </n-button>
          <n-button v-else size="small" @click="asking = true">反结账</n-button>
        </n-space>
      </div>

      <PeriodStrip
        :periods="info.periods || []"
        :model-value="period"
        @update:model-value="go"
      />

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

        <!-- 结账按钮灰着而不说为什么，是最容易被理解成「系统坏了」的一种状态。
             拦路的那几条本来就在自检里，但那是右栏第二个标签页，得先点开才看得见。 -->
        <n-alert
          v-else-if="blockers.length"
          type="error"
          :bordered="false"
          title="这个账期还结不了"
          style="margin-bottom: var(--s4)"
        >
          <div v-for="f in blockers" :key="f.id" class="small" style="margin-top: 2px">
            {{ f.name }}：{{ f.head || f.message }}
          </div>
          <div class="row" style="margin-top: var(--s3)">
            <n-button size="tiny" @click="rail = 'checks'">看自检明细</n-button>
            <n-button size="tiny" @click="fixing = true">怎么改</n-button>
          </div>
        </n-alert>

        <div class="cols">
          <div class="card" style="margin-top: 0">
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

          <div class="card rail" style="margin-top: 0">
            <n-tabs v-model:value="rail" type="line" size="small">
              <n-tab-pane name="gaps">
                <template #tab>
                  要看的
                  <n-badge
                    v-if="gaps.length"
                    :value="gaps.length"
                    :type="gaps.some((g) => g.severity === 'blocking') ? 'error' : 'warning'"
                    style="margin-left: 6px"
                  />
                </template>
                <p class="xs muted" style="margin-bottom: var(--s3)">
                  这个账期的空值项和异常项都在这儿。能点的点开就是它的来源明细。
                </p>
                <GapList :gaps="gaps" clickable @open="openGap" />
              </n-tab-pane>

              <n-tab-pane name="checks">
                <template #tab>
                  自检
                  <n-badge
                    v-if="bad.length"
                    :value="bad.length"
                    :type="bad.some((f) => f.blocking) ? 'error' : 'warning'"
                    style="margin-left: 6px"
                  />
                </template>
                <p class="xs muted" style="margin-bottom: var(--s3)">
                  拦路的那条不解决就结不了账，提示的那条不拦结账，但数字会有偏差。
                </p>
                <n-alert
                  v-for="f in bad"
                  :key="f.id"
                  :type="f.blocking ? 'error' : 'warning'"
                  :title="`${f.name}${f.blocking ? ' · 拦着结账' : ''}`"
                  :bordered="false"
                  style="margin-bottom: var(--s2)"
                >
                  {{ f.lines?.length ? f.head : f.message }}
                  <ul v-if="f.lines?.length" class="bullets">
                    <li v-for="(line, i) in f.lines" :key="i">{{ line }}</li>
                  </ul>
                </n-alert>
                <n-alert v-if="!bad.length" type="success" :bordered="false">
                  {{ (snap.findings || []).length }} 项检查都过了
                </n-alert>
              </n-tab-pane>

              <n-tab-pane name="sources">
                <template #tab>
                  该交的表
                  <n-badge
                    v-if="missingSources"
                    :value="missingSources"
                    type="warning"
                    style="margin-left: 6px"
                  />
                </template>
                <p class="xs muted" style="margin-bottom: var(--s2)">
                  缺一张，损益表上就有一行出不了数。
                </p>
                <div class="scroll">
                  <n-table size="small" :bordered="false">
                    <tbody>
                      <tr v-for="s in snap.sources || []" :key="s.id">
                        <td class="small">{{ s.name }}</td>
                        <td class="right">
                          <n-tag size="small" :type="s.arrived ? 'success' : 'warning'" :bordered="false">
                            {{ s.arrived ? '已交' : s.reason || '没交' }}
                          </n-tag>
                        </td>
                      </tr>
                    </tbody>
                  </n-table>
                </div>
              </n-tab-pane>

              <n-tab-pane v-if="snap.quality?.length" name="quality" tab="质量">
                <p class="xs muted" style="margin-bottom: var(--s2)">
                  挂钩是这张表有多少行认到了订单，覆盖是订单里有多少拿到了这项数。
                  百分比底下写的是它由哪两个数算出来的——光看 94% 不知道是差 7 笔还是差 767 笔。
                  画横杠的那格不是零，是这项不适用：全公司表不评挂钩，偶发科目不评覆盖。
                </p>
                <div class="scroll">
                  <n-table size="small" :bordered="false">
                    <thead>
                      <tr>
                        <th>项目</th>
                        <th class="right">表里行数</th>
                        <th class="right">挂钩</th>
                        <th class="right">覆盖</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="q in snap.quality" :key="q.metric">
                        <td class="small">
                          {{ q.name }}
                          <div v-if="q.company_wide || q.occasional" class="xs muted">
                            {{ q.company_wide ? '全公司表' : '' }}{{ q.company_wide && q.occasional ? ' · ' : '' }}{{ q.occasional ? '偶发科目' : '' }}
                          </div>
                        </td>
                        <td class="right num xs">{{ count(q.rows) }}</td>
                        <td class="right num xs">
                          {{ q.hit_rate === null ? '—' : percent(q.hit_rate) }}
                          <div v-if="q.hit_rate !== null" class="xs muted num">
                            {{ count(q.linked) }}/{{ count(q.rows) }}
                          </div>
                        </td>
                        <td class="right num xs">
                          {{ q.coverage === null ? '—' : percent(q.coverage) }}
                          <div v-if="q.coverage !== null" class="xs muted num">
                            {{ count(q.covered) }}/{{ count(q.expected) }}{{ q.expect_label ? ` ${q.expect_label}` : '' }}
                          </div>
                        </td>
                      </tr>
                    </tbody>
                  </n-table>
                </div>
              </n-tab-pane>

              <n-tab-pane v-if="snap.unlinked_buckets?.length" name="unlinked" tab="没进利润的钱">
                <p class="xs muted" style="margin-bottom: var(--s3)">
                  挂不上任何订单的钱，按「为什么挂不上」分开摆——处置完全不同。
                  排第一行的那类要人去查，灰掉的几类各有各的解释，摆出来是让人扫一眼确认「这些不用管」。
                </p>
                <div class="scroll">
                  <n-table size="small" :bordered="false">
                    <thead>
                      <tr>
                        <th>为什么挂不上</th>
                        <th class="right">笔数</th>
                        <th class="right">金额</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="(b, i) in unlinkedRows"
                        :key="i"
                        :class="{ quiet: unlinkedGraded && !b.counted }"
                      >
                        <td class="small">
                          {{ b.label }}
                          <div v-if="b.why" class="xs muted">{{ b.why }}</div>
                        </td>
                        <td class="right num xs">{{ count(b.count) }}</td>
                        <td class="right num">{{ money(b.amount) }}</td>
                      </tr>
                    </tbody>
                    <tfoot>
                      <tr>
                        <td class="small strong">要查归属的合计</td>
                        <td class="right num xs">
                          {{ unlinkedGraded ? count(unlinkedNeedsWork.count) : '—' }}
                        </td>
                        <td class="right num strong">{{ money(snap.unlinked_total) }}</td>
                      </tr>
                    </tfoot>
                  </n-table>
                </div>
                <p class="why" style="margin-top: var(--s3)">
                  合计只算「要查归属」那一类，所以它和上面几行加起来不相等。
                  几类相加得到的是一个没有业务含义的净额——别家店的运单、规则排除的非经营流水、
                  别的账期的订单收款，三者相减的巧合。真正要人查的只有
                  <b class="num">{{ money(snap.unlinked_total) }}</b>。
                </p>
              </n-tab-pane>
            </n-tabs>
          </div>
        </div>
      </template>

      <n-empty v-else description="这家店还没有算出来的账期" style="padding: var(--s7) 0">
        <template #extra><DropZone /></template>
      </n-empty>
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

    <FixPanel
      v-model:show="fixing"
      :store-id="props.id"
      :period="period"
      :unclassified="snap?.unclassified || []"
      @recompute="recompute"
    />

    <DrillDrawer
      v-if="drill"
      :run-id="drill.runId"
      :node="drill.node"
      :title="drill.name"
      @close="drill = null"
    />
  </n-spin>
</template>
