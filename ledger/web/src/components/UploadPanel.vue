<script setup>
/* 交表前的那一屏。
 *
 * 顶栏的「上传表格」以前直接弹系统选文件框，文件就这么没了——人问的是「我到底传到
 * 哪家店、哪个月了」。这套系统靠文件名认店、靠表里的日期认账期，本来就不需要人先
 * 选店再选月，可界面上一个字都没写，于是「不用选」看起来就像「没得选」。
 *
 * 这一屏只干一件事：在文件送出去之前把这两条规则讲清楚，并且把已登记的店名摊开——
 * 认店靠的就是这些名字，看得见才知道自己的文件名对不对得上。它不收任何必填项：
 * 自动识别是这套东西的卖点，不能因为要解释它就变成每次都要人选。
 */
import { useMessage } from 'naive-ui'
import { computed, ref } from 'vue'

import { useApp } from '../store'

const show = defineModel('show', { type: Boolean, default: false })

const app = useApp()
const message = useMessage()
const picker = ref(null)
const over = ref(false)

/** 认店认的是这些名字：店名本身，加上登记过的别名。 */
const names = computed(() =>
  app.stores.map((s) => ({
    id: s.id,
    name: s.name,
    aliases: s.aliases || [],
  })),
)

async function take(files) {
  const list = [...files].filter(Boolean)
  if (!list.length) return
  show.value = false
  try {
    await app.submit(list)
  } catch (e) {
    message.error(`没收下：${e.message}`, { duration: 6000 })
  }
}

function choose(e) {
  take(e.target.files)
  e.target.value = ''
}

function drop(e) {
  over.value = false
  take(e.dataTransfer?.files || [])
}
</script>

<template>
  <n-modal v-model:show="show" preset="card" title="上传表格" style="max-width: 640px">
    <div
      class="drop"
      :class="{ over }"
      @click="picker.click()"
      @dragover.prevent="over = true"
      @dragleave="over = false"
      @drop.prevent="drop"
    >
      <div class="strong" style="color: var(--n8)">把表拖到这儿，或者点一下选文件</div>
      <div class="small" style="margin-top: var(--s1)">
        订单明细、对账、运费、推广、代发、刷单都行，一次可以传多个
      </div>
      <input ref="picker" type="file" multiple hidden @change="choose" />
    </div>

    <section class="stack" style="margin-top: var(--s4)">
      <h3>传到哪家店、哪个月，不用你选</h3>
      <p class="small">
        <b>店铺看文件名</b>：文件名里出现哪家店的店名或别名，这份表就落到那家店。
        所以导出的文件名别改掉店名。
      </p>
      <p class="small">
        <b>账期看表里的日期</b>：解析出每一行的日期，落在哪个月就算哪个月的账。
        一份表跨了三个月，三个月的账都会跟着重算——不用拆开传。
      </p>
      <p class="small">
        <b>同一份表重复传不会算两遍</b>：内容一样就认出是同一份；内容变了就换掉旧的那份。
      </p>
    </section>

    <section class="stack" style="margin-top: var(--s4)">
      <h3>认得出的店名</h3>
      <p class="xs muted">
        文件名里对不上这些名字的，会在回执里单独列出来告诉你，不会悄悄进账。
      </p>
      <div class="chips">
        <span v-for="s in names" :key="s.id" class="chip">
          {{ s.name }}
          <template v-if="s.aliases.length">
            <span class="xs muted">（也认 {{ s.aliases.join('、') }}）</span>
          </template>
        </span>
      </div>
    </section>

    <section class="stack" style="margin-top: var(--s4)">
      <h3>没有「新建账期」这一步</h3>
      <p class="small">
        账期不是先建好再往里填的。有数据的月份自己会出现在账期清单里，
        没数据的月份和还没到的月份不在清单里——不是漏了，是还没有数据。
      </p>
    </section>

    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button size="small" @click="show = false">关掉</n-button>
      </div>
    </template>
  </n-modal>
</template>
