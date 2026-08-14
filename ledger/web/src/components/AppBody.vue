<script setup>
import { useMessage } from 'naive-ui'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import { useApp } from '../store'
import FilterBar from './FilterBar.vue'
import IntakeResult from './IntakeResult.vue'
import UploadPanel from './UploadPanel.vue'

const props = defineProps({ dropped: { type: Array, default: null } })
const emit = defineEmits(['taken'])

const app = useApp()
const message = useMessage()

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
    // 收完不跳页：人正开着某一页交表，被甩到别处是最讨厌的一种「帮忙」。算出来的
    // 账期在结果面板里列着，要去点一下就行。
    await app.submit(files)
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

const explaining = ref(false)

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
    </nav>

    <div class="body">
      <header class="topbar">
        <FilterBar />
        <!-- 上传只有这一个固定入口，每一页都在同一个地方。上一版侧栏最下角那个
             「交表」，位置和用词都在让人猜：交给谁、是不是报送、和结账什么关系。

             这里点开的是说明屏而不是直接弹选文件框：文件一旦送出去就没有再问
             「传到哪家店哪个月」的机会了，而这两件事恰恰是不用人选的——不说清楚，
             「不用选」看起来就是「没得选」。拖到窗口里的仍然直收，不经这一屏。 -->
        <n-button size="small" type="primary" @click="explaining = true">
          上传表格
        </n-button>
      </header>
      <main class="page">
        <router-view />
      </main>
    </div>

    <div v-if="app.busy" class="busy">
      <span class="spin" />
      <span>{{ app.busy.label }}</span>
      <!-- 阶段和百分比是这条提示存在的理由：转圈只能证明「还没返回」，证明不了
           「还在干活」。人分不出这两件事就会去刷新，一刷新这次交表的结果就没了。 -->
      <span v-if="app.busy.phase" class="dim">{{ app.busy.phase }}</span>
      <span v-if="app.busy.percent != null" class="num">{{ app.busy.percent }}%</span>
      <span class="num">{{ secs }}s</span>
      <span v-if="secs > 20" class="dim">别刷新</span>
    </div>

    <UploadPanel v-model:show="explaining" />
    <IntakeResult />
  </div>
</template>
