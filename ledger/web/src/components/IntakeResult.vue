<script setup>
/* 交表结果。
 *
 * 上一版交完表只弹一句「收下 3 份表」，三秒后消失。可这次交表真正要人知道的是
 * 另外几件事：哪份没进账、为什么、认不出的表怎么办、算出来的账期能不能结。这些
 * 后端一直都返回着，界面一个字都不显示——人只能默认「传了就是成功了」，然后拿
 * 一张少了运费表的账去结账。
 *
 * 所以这里把整份回执摊开，每一条都带着「下一步点哪儿」。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { useApp } from '../store'

const app = useApp()
const router = useRouter()

const it = computed(() => app.intake)
const kept = computed(() => it.value?.kept || [])
const fresh = computed(() => kept.value.filter((k) => !k.unchanged))
const same = computed(() => kept.value.filter((k) => k.unchanged))
const rejected = computed(() => it.value?.rejected || [])
const unknown = computed(() => it.value?.unknown_tables || [])
const periods = computed(() => it.value?.periods || [])
const failures = computed(() => it.value?.failures || [])

const storeName = computed(() =>
  Object.fromEntries(app.stores.map((s) => [s.id, s.name])),
)

/** 有没有需要人动手的事。有的话标题就不能只说「收下了」。 */
const todo = computed(
  () => rejected.value.length + unknown.value.length + failures.value.length,
)

function go(p) {
  app.showIntake = false
  router.push({ name: 'period', params: { id: p.store_id }, query: { period: p.period } })
}

function onboard(t) {
  app.showIntake = false
  router.push({ name: 'onboard', params: { sha: t.sha }, query: { sheet: t.sheet } })
}
</script>

<template>
  <n-modal
    v-model:show="app.showIntake"
    preset="card"
    :title="todo ? '收下了，但有几件事要你看一眼' : '收下了'"
    style="max-width: 720px"
  >
    <p class="small muted" style="margin-bottom: var(--s4)">{{ it?.summary }}</p>

    <section v-if="rejected.length" class="stack" style="margin-bottom: var(--s4)">
      <h3 class="warn">{{ rejected.length }} 份没进账</h3>
      <div v-for="r in rejected" :key="r.file" class="line">
        <span class="small strong">{{ r.file }}</span>
        <span class="xs muted">{{ r.why }}</span>
        <span v-if="r.suggest" class="xs">
          像是「{{ r.suggest }}」的表——文件名改成带这个店名再传一次
        </span>
      </div>
    </section>

    <section v-if="unknown.length" class="stack" style="margin-bottom: var(--s4)">
      <h3 class="warn">{{ unknown.length }} 张表没人认识</h3>
      <p class="xs muted">文件收下了，但这几张表不在任何模板里，里面的钱没进账。</p>
      <div v-for="t in unknown" :key="`${t.sha}${t.sheet}`" class="line">
        <span class="small">{{ t.file }} · {{ t.sheet }}</span>
        <n-button size="tiny" @click="onboard(t)">去接这张表</n-button>
      </div>
    </section>

    <section v-if="failures.length" class="stack" style="margin-bottom: var(--s4)">
      <h3 class="warn">{{ failures.length }} 家店没算出来</h3>
      <div v-for="(f, i) in failures" :key="i" class="line">
        <span class="small strong">{{ f.store }}</span>
        <span class="xs muted">{{ f.why }}</span>
      </div>
    </section>

    <section v-if="periods.length" style="margin-bottom: var(--s4)">
      <h3 style="margin-bottom: var(--s2)">算了 {{ periods.length }} 个账期</h3>
      <div class="scroll">
        <n-table size="small" :bordered="false">
          <thead>
            <tr>
              <th>店铺</th>
              <th>账期</th>
              <th>能不能结账</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in periods" :key="`${p.store_id}${p.period}`">
              <td class="xs">{{ p.store || storeName[p.store_id] || p.store_id }}</td>
              <td class="xs num">{{ p.period }}</td>
              <td class="xs">
                <span v-if="p.can_close">可以</span>
                <span v-else class="warn">还差自检</span>
              </td>
              <td><n-button size="tiny" quaternary @click="go(p)">打开</n-button></td>
            </tr>
          </tbody>
        </n-table>
      </div>
    </section>

    <section v-if="kept.length">
      <h3 style="margin-bottom: var(--s2)">收下的文件</h3>
      <p class="xs muted">
        {{ fresh.length }} 份是新的<template v-if="same.length">，{{ same.length }}
        份和上次一模一样，没有重复计算</template>。
      </p>
      <div class="scroll" style="margin-top: var(--s2)">
        <n-table size="small" :bordered="false">
          <tbody>
            <tr v-for="k in kept" :key="k.file">
              <td class="xs">{{ k.file }}</td>
              <td class="xs muted">{{ storeName[k.store_id] || k.store_id }}</td>
              <td class="xs muted">
                <template v-if="k.unchanged">和上次一样</template>
                <template v-else-if="k.replaced">换掉了旧的那份</template>
                <template v-else>新收</template>
              </td>
            </tr>
          </tbody>
        </n-table>
      </div>
    </section>

    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button size="small" @click="app.showIntake = false">知道了</n-button>
      </div>
    </template>
  </n-modal>
</template>
