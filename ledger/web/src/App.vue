<script setup>
/* 外壳：导航、筛选条、全局拖放、进度浮层。
 *
 * 拖放挂在整个窗口上而不是某个方框里。交表是这套系统里最高频的动作，让人先找到
 * 一个方框再松手是多出来的一步。
 */
import { NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'
import { computed, onMounted, onUnmounted, ref } from 'vue'

import AppBody from './components/AppBody.vue'
import { theme } from './theme'

const dragging = ref(0)
const overlay = computed(() => dragging.value > 0)
const dropped = ref(null)

function onEnter(e) {
  if (![...(e.dataTransfer?.types || [])].includes('Files')) return
  dragging.value += 1
}
function onLeave() {
  dragging.value = Math.max(0, dragging.value - 1)
}
function onOver(e) {
  e.preventDefault()
}
function onDrop(e) {
  e.preventDefault()
  dragging.value = 0
  const files = [...(e.dataTransfer?.files || [])]
  if (files.length) dropped.value = files
}

onMounted(() => {
  window.addEventListener('dragenter', onEnter)
  window.addEventListener('dragleave', onLeave)
  window.addEventListener('dragover', onOver)
  window.addEventListener('drop', onDrop)
})
onUnmounted(() => {
  window.removeEventListener('dragenter', onEnter)
  window.removeEventListener('dragleave', onLeave)
  window.removeEventListener('dragover', onOver)
  window.removeEventListener('drop', onDrop)
})
</script>

<template>
  <NConfigProvider :theme="null" :theme-overrides="theme">
    <NMessageProvider>
      <NDialogProvider>
        <AppBody :dropped="dropped" @taken="dropped = null" />
        <div v-if="overlay" class="veil">松手就收下</div>
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
