<script setup>
/* 交表框。窗口任何位置都能拖，这个框是给没想到可以拖的人留的入口。 */
import { useMessage } from 'naive-ui'
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { useApp } from '../store'

const app = useApp()
const message = useMessage()
const router = useRouter()
const picker = ref(null)

async function choose(e) {
  const files = [...e.target.files]
  e.target.value = ''
  if (!files.length) return
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
  } catch (err) {
    message.error(`没收下：${err.message}`, { duration: 6000 })
  }
}
</script>

<template>
  <div class="drop" @click="picker.click()">
    <div class="strong" style="color: var(--n8)">把表拖进来</div>
    <div class="small" style="margin-top: var(--s1)">
      订单明细、对账、运费、推广都行，一次可以传多个。店铺和账期从文件名认。
    </div>
    <input ref="picker" type="file" multiple hidden @change="choose" />
  </div>
</template>
