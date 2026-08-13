<script setup>
/* 检索结果。
 *
 * 人来这里通常只有一样东西：一个订单号，或者一个对不上的金额。要的答案是
 * 「它在哪个文件的哪一行」——所以每条结果都必须给到文件、工作表、行号，
 * 少一样都还得回去翻表。
 */
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import { money } from '../format'
import { useApp } from '../store'

const props = defineProps({
  show: { type: Boolean, default: false },
  term: { type: String, default: '' },
})
const emit = defineEmits(['update:show'])

const app = useApp()
const loading = ref(false)
const error = ref('')
const result = ref(null)

const KIND = { order: '按订单号', amount: '按金额', text: '按文字' }

const how = computed(() =>
  (result.value?.kinds || []).map((k) => KIND[k] || k).join('、'),
)

watch(
  () => [props.show, props.term],
  async ([show, term]) => {
    if (!show || !term.trim()) return
    loading.value = true
    error.value = ''
    result.value = null
    try {
      result.value = await api.search({
        q: term.trim(),
        store_id: app.storeId,
        period: app.period,
        platform: app.platform,
      })
    } catch (e) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)
</script>

<template>
  <n-drawer
    :show="show"
    :width="720"
    placement="right"
    @update:show="(v) => emit('update:show', v)"
  >
    <n-drawer-content :title="`检索「${term}」`" closable>
      <n-spin :show="loading">
        <n-alert v-if="error" type="error" :bordered="false">{{ error }}</n-alert>

        <template v-else-if="result">
          <div class="spread" style="margin-bottom: var(--s4)">
            <div class="muted small">
              {{ how }}，命中 <span class="num">{{ result.total }}</span> 行
              <template v-if="result.amount">
                ，合计 <span class="num">{{ money(result.amount) }}</span>
              </template>
            </div>
            <n-tag v-if="result.truncated" size="small" type="warning">只列了前几条</n-tag>
          </div>

          <n-alert
            v-for="(note, i) in result.notes || []"
            :key="i"
            type="warning"
            :bordered="false"
            style="margin-bottom: var(--s3)"
          >
            {{ note }}
          </n-alert>

          <template v-if="result.total">
            <n-table v-if="result.by_store?.length" size="small" :bordered="false">
              <thead>
                <tr>
                  <th>店铺</th>
                  <th>账期</th>
                  <th class="right">行数</th>
                  <th class="right">合计</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(s, i) in result.by_store" :key="i">
                  <td>{{ s.store }}</td>
                  <td class="num">{{ s.period }}</td>
                  <td class="right num">{{ s.rows }}</td>
                  <td class="right num">{{ money(s.amount) }}</td>
                </tr>
              </tbody>
            </n-table>

            <h3 style="margin: var(--s5) 0 var(--s2)">命中的行</h3>
            <n-table size="small" :bordered="false">
              <thead>
                <tr>
                  <th>科目</th>
                  <th class="right">金额</th>
                  <th>在哪一行</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(h, i) in result.hits" :key="i">
                  <td>
                    {{ h.subject || h.metric || '—' }}
                    <div class="xs muted">{{ h.store }} · {{ h.period }}</div>
                  </td>
                  <td class="right num" :class="{ neg: h.amount < 0 }">{{ money(h.amount) }}</td>
                  <td class="xs num">
                    {{ h.file }}
                    <template v-if="h.sheet"> · {{ h.sheet }}</template>
                    · 第 {{ h.row_no }} 行
                  </td>
                </tr>
              </tbody>
            </n-table>
          </template>

          <n-empty v-else description="没找到">
            <template #extra>
              <div class="small muted" style="max-width: 380px">
                订单号要完整；金额可以带正负号和千分位，正负都会试；科目和文件名按包含匹配。
              </div>
            </template>
          </n-empty>
        </template>
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>
