<script setup>
/* 一家店的账期切换。
 *
 * 不要做成四十个蓝按钮：那是工具栏，不是账本。月份是这页的「现在看哪一本」，
 * 应当像翻书——当前月写大，其余按年收成一条细轨道。状态只标在眼前这一本上，
 * 每个月都点一颗红点，人就看不见哪颗是真的在拦结账。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])

const list = computed(() =>
  [...(props.periods || [])].sort((a, b) => String(b.period).localeCompare(String(a.period))),
)

const index = computed(() => list.value.findIndex((p) => p.period === props.modelValue))

const current = computed(() => list.value[index.value] || null)

function yearOf(period) {
  return /^\d{4}/.test(period || '') ? period.slice(0, 4) : '其他'
}

function pretty(period) {
  const m = /^(\d{4})-(\d{2})$/.exec(period || '')
  if (!m) return period || ''
  return `${m[1]}年${Number(m[2])}月`
}

function monthOf(period) {
  const m = /^(\d{4})-(\d{2})$/.exec(period || '')
  if (!m) return period || ''
  return `${Number(m[2])}月`
}

const years = computed(() => {
  const map = new Map()
  for (const p of list.value) {
    const y = yearOf(p.period)
    if (!map.has(y)) map.set(y, [])
    map.get(y).push(p)
  }
  return [...map.entries()].map(([year, months]) => ({ year, months }))
})

const shownYear = ref('')

watch(
  () => [props.modelValue, years.value],
  () => {
    const y = yearOf(props.modelValue)
    if (years.value.some((row) => row.year === y)) shownYear.value = y
    else if (!shownYear.value && years.value.length) shownYear.value = years.value[0].year
  },
  { immediate: true },
)

const months = computed(
  () => years.value.find((row) => row.year === shownYear.value)?.months || [],
)

function go(p) {
  if (p && p !== props.modelValue) emit('update:modelValue', p)
}

function step(dir) {
  const next = list.value[index.value + dir]
  if (next) go(next.period)
}

function pickYear(year) {
  shownYear.value = year
  const row = years.value.find((r) => r.year === year)
  if (!row?.months.some((p) => p.period === props.modelValue)) {
    go(row.months[0]?.period)
  }
}

function statusOf(p) {
  if (!p) return { mark: '', text: '' }
  if (p.state === 'closed') return { mark: 'ok', text: '已结' }
  if (p.can_close === false) return { mark: 'bad', text: '结不了' }
  return { mark: '', text: '未结' }
}

const status = computed(() => statusOf(current.value))
</script>

<template>
  <div v-if="list.length" class="book">
    <div class="hero">
      <button
        class="nav"
        type="button"
        :disabled="index <= 0"
        title="较新的一个月"
        @click="step(-1)"
      >
        ‹
      </button>
      <div class="now">
        <div class="when">{{ pretty(modelValue) }}</div>
        <div v-if="status.text" class="tag" :class="status.mark">{{ status.text }}</div>
      </div>
      <button
        class="nav"
        type="button"
        :disabled="index < 0 || index >= list.length - 1"
        title="更早的一个月"
        @click="step(1)"
      >
        ›
      </button>
    </div>

    <div class="rail">
      <div class="years" role="tablist" aria-label="年份">
        <button
          v-for="row in years"
          :key="row.year"
          type="button"
          class="year"
          :class="{ on: row.year === shownYear }"
          @click="pickYear(row.year)"
        >
          {{ row.year }}
        </button>
      </div>
      <div class="months" role="tablist" aria-label="月份">
        <button
          v-for="p in months"
          :key="p.period"
          type="button"
          class="month"
          :class="{ on: p.period === modelValue }"
          :title="`${pretty(p.period)} · ${statusOf(p).text}`"
          @click="go(p.period)"
        >
          {{ monthOf(p.period) }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.book {
  margin: 0 0 var(--s5);
  padding-bottom: var(--s4);
  border-bottom: 1px solid var(--n2);
}
.hero {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--s4);
  margin-bottom: var(--s4);
}
.now {
  display: flex;
  align-items: baseline;
  gap: var(--s3);
  min-width: 168px;
  justify-content: center;
}
.when {
  font-family: var(--num);
  font-size: var(--t-2xl);
  font-weight: 620;
  letter-spacing: -.03em;
  line-height: 1.1;
}
.tag {
  font-size: var(--t-xs);
  color: var(--n6);
  padding: 1px 8px;
  border-radius: 99px;
  background: var(--n2);
}
.tag.ok { color: var(--ok); background: var(--ok-bg); }
.tag.bad { color: var(--bad); background: var(--bad-bg); }

.nav {
  width: 36px;
  height: 36px;
  border: 1px solid var(--n3);
  border-radius: 50%;
  background: var(--n0);
  color: var(--n7);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  transition: border-color .15s, color .15s;
}
.nav:hover:not(:disabled) {
  border-color: var(--n7);
  color: var(--n9);
}
.nav:disabled {
  opacity: .28;
  cursor: default;
}

.rail {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--s5);
  flex-wrap: wrap;
}
.years,
.months {
  display: flex;
  align-items: center;
  gap: 2px;
}
.year,
.month {
  border: 0;
  background: transparent;
  color: var(--n6);
  cursor: pointer;
  font: inherit;
  line-height: 1;
  padding: 6px 10px;
  border-radius: 99px;
}
.year {
  font-size: var(--t-xs);
  font-family: var(--num);
  letter-spacing: .04em;
}
.month {
  font-size: var(--t-sm);
  min-width: 44px;
}
.year:hover,
.month:hover { color: var(--n9); background: var(--n1); }
.year.on {
  color: var(--n9);
  background: var(--n2);
  font-weight: 560;
}
.month.on {
  color: var(--accent);
  background: var(--accent-bg);
  font-weight: 600;
}
</style>
