<script setup>
/* 「这个数不对，我怎么改」。
 *
 * 这套账里没有「改单元格」这个动作，是有意的：每个数都要能一路回溯到某个文件的某一行，
 * 手改一笔，这条链就断了，而且断得看不出来——下个月同样的问题还会再来一次，因为源头
 * 没动过。所以能改的只有两样东西：交上来的表，和认表的口径。
 *
 * 但「不给改」不等于「没得改」。人看到一项是空的、或者算出来是负的，需要的是一条明确
 * 的路：先定位到是哪张表的哪一行，再判断是表错了还是口径错了，改完重算。这一屏就是
 * 把这条路摆出来，每一步都带着能点的入口——只写一段说明文字的话，人还是得自己找。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { count, money } from '../format'

const props = defineProps({
  storeId: { type: String, default: '' },
  period: { type: String, default: '' },
  //: 这个账期没认出来的科目。「口径不对」最常见的一种就是它，而且它是唯一
  //: 能直接指出「该补哪一条」的线索，所以摆在这儿而不是让人去别处找。
  unclassified: { type: Array, default: () => [] },
})
const show = defineModel('show', { type: Boolean, default: false })
const emit = defineEmits(['recompute'])

const router = useRouter()

const odd = computed(() => props.unclassified.slice(0, 8))

function toStores() {
  show.value = false
  router.push({ name: 'deliver' })
}

function recompute() {
  show.value = false
  emit('recompute')
}
</script>

<template>
  <n-modal v-model:show="show" preset="card" title="这个数不对，怎么改" style="max-width: 680px">
    <n-alert type="default" :bordered="false" style="margin-bottom: var(--s4)">
      这里不能直接改数字。账上每一笔都要能指回某个文件的某一行，手改一笔这条链就断了，
      而且源头没动，下个月还会再错一次。能改的是两样：<b>交上来的表</b>，和<b>认表的口径</b>。
    </n-alert>

    <section class="stack" style="margin-bottom: var(--s4)">
      <h3>第一步 · 先看这个数是哪来的</h3>
      <p class="small">
        点损益表上那一行，右边抽屉里是它的全部构成：按科目、按来源文件分好，
        再往下是原始行，每行写着<b>文件名 · 工作表 · 第几行</b>。
        拿这个行号回去翻源文件，就能对上是哪一笔。
      </p>
      <p class="xs muted">
        抽屉里可以按订单号或科目筛，也可以切到「没进账」看这张表里没算进这家店的那部分。
      </p>
    </section>

    <section class="stack" style="margin-bottom: var(--s4)">
      <h3>第二步 · 判断是表的问题还是口径的问题</h3>
      <p class="small">
        <b>表传错了、传漏了、传的是旧版本</b> —— 去「数据与店铺」把那份撤下来，改好再传一次。
        撤下和重传都留痕：谁传的、什么时候、换掉了哪一版，都记着。
      </p>
      <p class="small">
        <b>表是对的，是系统没认出来</b> —— 分三种：科目字典里没有（下面列着）、
        整张表没人认识（交表回执里会让你去接这张表）、文件名里的店名没登记（去加个别名）。
      </p>
      <p class="small">
        <b>数就是这样</b> —— 比如退款月负数、跨期结算收款落在别的月。这种不用改，
        右栏「没进利润的钱」和「要看的」里写着为什么。
      </p>
      <div class="row" style="margin-top: var(--s2)">
        <n-button size="small" @click="toStores">去撤表 / 重传 / 加别名</n-button>
      </div>
    </section>

    <section v-if="odd.length" class="stack" style="margin-bottom: var(--s4)">
      <h3>这个账期有 {{ count(unclassified.length) }} 个科目字典里没有</h3>
      <p class="xs muted">
        没认出来的科目不进任何一项，所以它们的钱既不在收入里也不在费用里。
        补字典要改模型配置（<span class="num">dictionary.csv</span>），
        目前得在服务器上改，改完回这儿点重算。
      </p>
      <div class="scroll">
        <n-table size="small" :bordered="false">
          <thead>
            <tr>
              <th>原始科目</th>
              <th class="right">笔数</th>
              <th class="right">金额</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(u, i) in odd" :key="i">
              <td class="xs">{{ u.label }}</td>
              <td class="right xs num">{{ count(u.count) }}</td>
              <td class="right xs num" :class="{ neg: u.amount < 0 }">{{ money(u.amount) }}</td>
            </tr>
          </tbody>
        </n-table>
      </div>
    </section>

    <section class="stack">
      <h3>第三步 · 重算，然后结账</h3>
      <p class="small">
        表或口径动过之后要重算一次，账上的数才会跟着变。结账之后数字就定死了；
        发现结错了可以反结账，但必须写清为什么——这条会记进账期历史，谁在什么时候
        因为什么改回去的，一直留着。
      </p>
      <div class="row" style="margin-top: var(--s2)">
        <n-button size="small" type="primary" @click="recompute">重算这家店</n-button>
      </div>
    </section>

    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button size="small" @click="show = false">知道了</n-button>
      </div>
    </template>
  </n-modal>
</template>
