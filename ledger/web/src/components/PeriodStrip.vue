<script setup>
/* 一家店的账期切换。
 *
 * 曾经是一排自动换行的按钮：四十多个月、每个都写着「· 结不了」，占掉半屏，
 * 真正要看的损益表被挤到下面。顶栏已经有一个全局账期下拉，这里再铺一遍历史
 * 等于同一件事做了两次，而且两次的选中还容易对不上。
 *
 * 所以改成单行横滑：左右键切相邻月，轨道上滚轮/拖动看更早的账期，当前月
 * 自动滚到中间。不另引 Swiper 这类库——账期就几十个，原生 overflow 够用，
 * 多一个依赖只为这一个条，更新和包体都不值。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])

const scroller = ref(null)

const list = computed(() =>
  [...(props.periods || [])].sort((a, b) => String(b.period).localeCompare(String(a.period))),
)

const index = computed(() => list.value.findIndex((p) => p.period === props.modelValue))

const chips = computed(() => {
  const out = []
  let year = null
  for (const p of list.value) {
    const y = /^\d{4}/.test(p.period) ? p.period.slice(0, 4) : ''
    if (y && y !== year) {
      year = y
      out.push({ kind: 'year', key: `y-${y}`, year: y })
    }
    out.push({ kind: 'period', key: p.period, ...p })
  }
  return out
})

function go(p) {
  if (p && p !== props.modelValue) emit('update:modelValue', p)
}

function step(dir) {
  const i = index.value
  if (i < 0) return
  const next = list.value[i + dir]
  if (next) go(next.period)
}

function statusOf(p) {
  if (p.state === 'closed') return { mark: 'ok', text: '已结' }
  if (p.can_close === false) return { mark: 'bad', text: '结不了' }
  return { mark: '', text: '未结' }
}

function scrollCurrent() {
  nextTick(() => {
    const root = scroller.value
    const el = root?.querySelector('[data-current="1"]')
    if (!root || !el) return
    const er = el.getBoundingClientRect()
    const rr = root.getBoundingClientRect()
    root.scrollBy({ left: er.left - rr.left - (rr.width - er.width) / 2, behavior: 'smooth' })
  })
}

function onWheel(e) {
  const el = scroller.value
  if (!el || el.scrollWidth <= el.clientWidth) return
  if (e.deltaY === 0) return
  el.scrollLeft += e.deltaY
  e.preventDefault()
}

watch(() => props.modelValue, scrollCurrent)
onMounted(() => {
  scrollCurrent()
  const el = scroller.value
  if (!el) return
  el.addEventListener('wheel', onWheel, { passive: false })
  onUnmounted(() => el.removeEventListener('wheel', onWheel))
})
</script>

<template>
  <div class="strip">
    <n-button
      size="small"
      quaternary
      :disabled="index <= 0"
      title="较新的一个月"
      @click="step(-1)"
    >
      ‹
    </n-button>
    <div ref="scroller" class="track">
      <template v-for="c in chips" :key="c.key">
        <span v-if="c.kind === 'year'" class="year">{{ c.year }}</span>
        <n-button
          v-else
          size="small"
          :type="c.period === modelValue ? 'primary' : 'default'"
          :data-current="c.period === modelValue ? '1' : '0'"
          :title="`${c.period} · ${statusOf(c).text}`"
          @click="go(c.period)"
        >
          <span class="num">{{ c.period }}</span>
          <span v-if="c.period === modelValue" class="flag">{{ statusOf(c).text }}</span>
          <span
            v-else-if="statusOf(c).mark"
            class="dot"
            :class="statusOf(c).mark"
          />
        </n-button>
      </template>
    </div>
    <n-button
      size="small"
      quaternary
      :disabled="index < 0 || index >= list.length - 1"
      title="更早的一个月"
      @click="step(1)"
    >
      ›
    </n-button>
  </div>
</template>

<style scoped>
.strip {
  display: flex;
  align-items: center;
  gap: var(--s2);
  margin-bottom: var(--s4);
  min-width: 0;
}
.track {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--s2);
  overflow-x: auto;
  scrollbar-width: thin;
  padding: 2px 0;
  scroll-snap-type: x proximity;
}
.track > :deep(.n-button) {
  flex: 0 0 auto;
  scroll-snap-align: center;
}
.year {
  flex: 0 0 auto;
  font-size: var(--t-xs);
  color: var(--n5);
  padding: 0 2px 0 6px;
}
.num { font-family: var(--num); }
.flag {
  margin-left: 6px;
  font-size: var(--t-xs);
}
.dot {
  width: 6px;
  height: 6px;
  margin-left: 6px;
  border-radius: 50%;
  display: inline-block;
  vertical-align: middle;
}
.dot.ok { background: var(--ok); }
.dot.bad { background: var(--bad); }
</style>
