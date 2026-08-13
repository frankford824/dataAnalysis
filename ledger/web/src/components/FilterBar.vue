<script setup>
/* 平台 / 店铺 / 账期 + 检索。
 *
 * 这条要一直横在顶上，所有页面共用一套选择。上一版每页各管各的，从展板点进一家店
 * 再切到数据交付，选中的店就没了——多店铺时每切一次页要重选一次，等于不能用。
 *
 * 检索在这里而不是单开一页：人要查一个订单号的时候，手上正开着某一页，不该被
 * 赶去另一个地方再回来。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import { useApp } from '../store'
import SearchPanel from './SearchPanel.vue'

const app = useApp()
const router = useRouter()

const searching = ref(false)
const term = ref('')

const platformOptions = computed(() => [
  { label: '全部平台', value: '' },
  ...app.platforms.map((p) => ({ label: p.name, value: p.id })),
])

const storeOptions = computed(() => [
  { label: '全部店铺', value: '' },
  ...app.visibleStores.map((s) => ({ label: s.name, value: s.id })),
])

const periodOptions = computed(() => [
  { label: '全部账期', value: '' },
  ...app.periods.map((p) => ({ label: p, value: p })),
])

function onStore(id) {
  app.pick({ store: id })
  // 选了店就直接去那家店的账期页——选完还要再点一次才有反应是多余的一步。
  if (id) router.push({ name: 'period', params: { id }, query: { period: app.period } })
}

function submit() {
  if (term.value.trim()) searching.value = true
}
</script>

<template>
  <div class="row wrap" style="gap: var(--s2)">
    <n-select
      :value="app.platform"
      :options="platformOptions"
      size="small"
      style="width: 132px"
      @update:value="(v) => app.pick({ platform: v })"
    />
    <n-select
      :value="app.storeId"
      :options="storeOptions"
      size="small"
      filterable
      style="width: 180px"
      @update:value="onStore"
    />
    <n-select
      :value="app.period"
      :options="periodOptions"
      size="small"
      style="width: 132px"
      @update:value="(v) => app.pick({ period: v })"
    />

    <div class="grow" />

    <n-input
      v-model:value="term"
      size="small"
      placeholder="订单号、金额、科目、文件名"
      style="width: 260px"
      clearable
      @keyup.enter="submit"
    />
    <n-button size="small" :disabled="!term.trim()" @click="submit">查</n-button>

    <SearchPanel v-model:show="searching" :term="term" />
  </div>
</template>
