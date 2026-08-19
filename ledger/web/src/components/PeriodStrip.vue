<script setup>
/* 一家店的账期切换。
 *
 * 摆法照日历来：翻页键在最左，后面是当前月份，底下先选年、再从一到十二挑月。
 * 月份不按「新的在前」倒着排——那是列表的逻辑，不是日历的逻辑，人看一月在最
 * 右边会先愣一下。
 *
 * 没算过的月份留在原位、点不动。这样一年里哪几个月还没账，扫一眼就知道；
 * 只列算过的那几个月，缺口就看不见了。
 *
 * 状态只标在眼前这一本上。每个月都挂个红点的话，真正拦着结账的那个月就不显眼了。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])

//: 认不出年月的账期（「(未知账期)」）归到这一档，不能让它挤进某一年的十二格里。
const OTHER = '其他'

function yearOf(period) {
  return /^\d{4}-\d{2}$/.test(period || '') ? period.slice(0, 4) : OTHER
}

function pretty(period) {
  const m = /^(\d{4})-(\d{2})$/.exec(period || '')
  return m ? `${m[1]} 年 ${Number(m[2])} 月` : period || ''
}

const list = computed(() =>
  [...(props.periods || [])].sort((a, b) => String(b.period).localeCompare(String(a.period))),
)

const index = computed(() => list.value.findIndex((p) => p.period === props.modelValue))
const current = computed(() => list.value[index.value] || null)
const byPeriod = computed(() => new Map(list.value.map((p) => [p.period, p])))

const years = computed(() => {
  const out = []
  for (const p of list.value) {
    const y = yearOf(p.period)
    if (!out.includes(y)) out.push(y)
  }
  return out
})

const shownYear = ref('')

watch(
  () => [props.modelValue, years.value],
  () => {
    const y = yearOf(props.modelValue)
    if (years.value.includes(y)) shownYear.value = y
    else if (!years.value.includes(shownYear.value)) shownYear.value = years.value[0] || ''
  },
  { immediate: true },
)

const months = computed(() => {
  if (shownYear.value === OTHER) {
    return list.value
      .filter((p) => yearOf(p.period) === OTHER)
      .map((p) => ({ key: p.period, label: p.period || '未知', period: p.period, has: true }))
  }
  return Array.from({ length: 12 }, (_, i) => {
    const period = `${shownYear.value}-${String(i + 1).padStart(2, '0')}`
    return { key: period, label: `${i + 1}`, period, has: byPeriod.value.has(period) }
  })
})

function go(p) {
  if (p && p !== props.modelValue) emit('update:modelValue', p)
}

function step(dir) {
  const next = list.value[index.value + dir]
  if (next) go(next.period)
}

function pickYear(year) {
  shownYear.value = year
  if (yearOf(props.modelValue) !== year) {
    go(list.value.find((p) => yearOf(p.period) === year)?.period)
  }
}

function statusOf(p) {
  if (!p) return { mark: '', text: '' }
  if (p.state === 'closed') return { mark: 'ok', text: '已结账' }
  if (p.can_close === false) return { mark: 'bad', text: '结不了' }
  return { mark: 'idle', text: '未结账' }
}

const status = computed(() => statusOf(current.value))
</script>

<template>
  <div v-if="list.length" class="periods">
    <div class="head">
      <div class="pager">
        <button
          type="button"
          :disabled="index <= 0"
          title="较新的一个月"
          aria-label="较新的一个月"
          @click="step(-1)"
        >
          ‹
        </button>
        <button
          type="button"
          :disabled="index < 0 || index >= list.length - 1"
          title="更早的一个月"
          aria-label="更早的一个月"
          @click="step(1)"
        >
          ›
        </button>
      </div>
      <div class="when">{{ pretty(modelValue) }}</div>
      <div v-if="status.text" class="state" :class="status.mark">
        <i />{{ status.text }}
      </div>
    </div>

    <div class="pick">
      <div class="years">
        <button
          v-for="y in years"
          :key="y"
          type="button"
          :class="{ on: y === shownYear }"
          @click="pickYear(y)"
        >
          {{ y }}
        </button>
      </div>
      <span v-if="shownYear !== OTHER" class="sep" />
      <div class="months" :class="{ free: shownYear === OTHER }">
        <button
          v-for="m in months"
          :key="m.key"
          type="button"
          :class="{ on: m.period === modelValue, off: !m.has }"
          :disabled="!m.has"
          :title="m.has ? `${pretty(m.period)} · ${statusOf(byPeriod.get(m.period)).text}` : '这个月还没算过'"
          @click="go(m.period)"
        >
          {{ m.label }}
        </button>
        <span v-if="shownYear !== OTHER" class="unit">月</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.periods {
  margin-bottom: var(--s5);
  padding-bottom: var(--s4);
  border-bottom: 1px solid var(--n2);
}

.head {
  display: flex;
  align-items: center;
  gap: var(--s3);
  margin-bottom: var(--s3);
}

.pager { display: flex; gap: var(--s1); }
.pager button {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--n3);
  border-radius: var(--r-sm);
  background: var(--n0);
  color: var(--n6);
  font-size: 17px;
  line-height: 1;
  cursor: pointer;
  transition: color .15s, border-color .15s;
}
.pager button:hover:not(:disabled) { color: var(--n9); border-color: var(--n5); }
.pager button:disabled { opacity: .35; cursor: default; }

.when {
  font-family: var(--num);
  font-size: var(--t-xl);
  font-weight: 620;
  letter-spacing: -.01em;
  line-height: 1.2;
}

/* 状态是一句注解，不是一枚徽章。做成实心色块会跟报表里真正要人看的红色抢注意力。 */
.state {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--t-sm);
  color: var(--n6);
}
.state i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.state.ok { color: var(--ok); }
.state.bad { color: var(--bad); }
.state.idle { color: var(--n5); }

.pick {
  display: flex;
  align-items: center;
  gap: var(--s3);
  flex-wrap: wrap;
}
.sep {
  width: 1px;
  height: 14px;
  background: var(--n3);
}

.years, .months { display: flex; align-items: center; gap: 2px; }

.years button,
.months button {
  border: 0;
  background: transparent;
  color: var(--n6);
  font: inherit;
  line-height: 1;
  cursor: pointer;
  border-radius: var(--r-sm);
  transition: color .12s, background .12s;
}
.years button {
  font-family: var(--num);
  font-size: var(--t-xs);
  letter-spacing: .03em;
  padding: 5px 8px;
}
.months button {
  font-family: var(--num);
  font-size: var(--t-sm);
  min-width: 30px;
  padding: 5px 0;
}
.months.free button {
  min-width: 0;
  padding: 5px 8px;
}
.years button:hover,
.months button:hover:not(:disabled) { color: var(--n9); background: var(--n1); }

.years button.on { color: var(--n9); background: var(--n2); font-weight: 560; }
.months button.on { color: var(--n0); background: var(--n8); font-weight: 560; }
/* 没算过的月份：淡到不像能点，但位置还占着——缺哪个月是要看见的。 */
.months button.off { color: var(--n4); cursor: default; }

.unit {
  font-size: var(--t-xs);
  color: var(--n5);
  padding-left: 4px;
}
</style>
