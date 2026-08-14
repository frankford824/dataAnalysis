<script setup>
/* 空空如也的地方摆的那个上传框。
 *
 * 只在「这儿本来该有数据但一份都没有」的时候出现。有数据的页面不再摆它：常驻的
 * 上传入口在顶栏，位置固定，每页都一样；文件拖到窗口任何位置也收。上一版每页都
 * 塞一个，有的在内容前面有的在后面，同一个动作在三个页面长在三个地方。
 */
import { useMessage } from 'naive-ui'
import { ref } from 'vue'

import { useApp } from '../store'

const app = useApp()
const message = useMessage()
const picker = ref(null)

async function choose(e) {
  const files = [...e.target.files]
  e.target.value = ''
  if (!files.length) return
  try {
    await app.submit(files)
  } catch (err) {
    message.error(`没收下：${err.message}`, { duration: 6000 })
  }
}
</script>

<template>
  <div class="drop" @click="picker.click()">
    <div class="strong" style="color: var(--n8)">把表拖进来</div>
    <!-- 「从文件名认」曾经只写了半句，人看完仍然要问账期怎么定。店和账期是两条
         不同的规则，各认各的东西，缺一条就得靠猜。 -->
    <div class="small" style="margin-top: var(--s1)">
      订单明细、对账、运费、推广都行，一次可以传多个。
    </div>
    <div class="xs muted" style="margin-top: var(--s1)">
      店铺看文件名里的店名，账期看表里的日期——不用先选店、也不用先建月份。
    </div>
    <input ref="picker" type="file" multiple hidden @change="choose" />
  </div>
</template>
