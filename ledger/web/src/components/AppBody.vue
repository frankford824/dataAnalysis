<script setup>
import { useMessage } from 'naive-ui'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { useApp } from '../store'
import FilterBar from './FilterBar.vue'

const props = defineProps({ dropped: { type: Array, default: null } })
const emit = defineEmits(['taken'])

const app = useApp()
const message = useMessage()
const router = useRouter()
const picker = ref(null)

const openCount = computed(
  () => (app.overview?.cells || []).filter((c) => c.state === 'open').length,
)

// 上传要多久取决于表有多大，淘宝一个月的表能跑十几秒。不显示已用秒数的话，人会
// 以为卡死了，然后刷新——刷新会让这次上传的结果看不见。
const secs = ref(0)
let tick = null
watch(
  () => app.busy,
  (busy) => {
    clearInterval(tick)
    secs.value = 0
    if (busy) tick = setInterval(() => (secs.value = Math.round((Date.now() - busy.since) / 1000)), 1000)
  },
)
onUnmounted(() => clearInterval(tick))

async function take(files) {
  if (!files?.length) return
  try {
    const res = await app.upload(files)
    message.success(res.summary || '收下了')
    await app.load(true)
    const last = res.periods?.[res.periods.length - 1]
    if (last?.store_id) {
      router.push({
        name: 'period',
        params: { id: last.store_id },
        query: { period: last.period },
      })
    }
  } catch (e) {
    message.error(`没收下：${e.message}`, { duration: 6000 })
  }
}

watch(
  () => props.dropped,
  (files) => {
    if (files?.length) {
      take(files)
      emit('taken')
    }
  },
)

function choose(e) {
  take([...e.target.files])
  e.target.value = ''
}

onMounted(() => {
  app.load().catch((e) => message.error(e.message, { duration: 6000 }))
})

defineExpose({ take })
</script>

<template>
  <div class="shell">
    <nav class="side">
      <div class="brand">记账</div>
      <router-link class="navlink" :class="{ on: $route.name === 'board' }" to="/">
        总览<span v-if="openCount" class="count">{{ openCount }}</span>
      </router-link>
      <router-link class="navlink" :class="{ on: $route.name === 'deliver' }" to="/deliver">
        数据与店铺<span class="count">{{ app.stores.length || '' }}</span>
      </router-link>
      <router-link
        class="navlink"
        :class="{ on: $route.name === 'commission' }"
        to="/commission"
      >
        提成
      </router-link>
      <div class="grow" />
      <n-button size="small" block @click="picker.click()">交表</n-button>
      <input ref="picker" type="file" multiple hidden @change="choose" />
    </nav>

    <div class="body">
      <header class="topbar">
        <FilterBar />
      </header>
      <main class="page">
        <router-view />
      </main>
    </div>

    <div v-if="app.busy" class="busy">
      <span class="spin" />
      <span>{{ app.busy.label }}</span>
      <span class="num">{{ secs }}s</span>
      <span v-if="secs > 20" class="dim">别刷新</span>
    </div>
  </div>
</template>
