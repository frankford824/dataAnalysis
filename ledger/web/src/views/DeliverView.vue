<script setup>
/* 数据与店铺，一页。
 *
 * 原来这是两页。店铺页上摆着主体、税号、等级这些东西，而它们一年也改不了一次；
 * 真正每周要做的事——这家店的表交齐了没、哪张是旧的、传错了要撤下来——反而分散在
 * 另一页。合成一页之后，一家店就是一张卡：它是哪个平台的、交了哪些表、算到哪个
 * 账期了。
 *
 * 摆法是左边一列店、右边一张表。上一版是每家店一张卡竖着铺，十几家店就是十几屏，
 * 想比较「淘宝那家交齐了没、抖音那家呢」得来回滚。左右分栏之后换店只是点一下，
 * 右边那半屏原地换掉，人的视线不用移动。
 *
 * 登记新店只要店名加平台。剩下的字段系统自己能认：账期从文件名认，店铺主体在
 * 需要开票之前根本用不上。
 */
import { useDialog, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import DropZone from '../components/DropZone.vue'
import { ago, bytes, count, money } from '../format'
import { useApp } from '../store'

const app = useApp()
const message = useMessage()
const dialog = useDialog()
const router = useRouter()

const detail = ref({})
const loading = ref(false)
const adding = ref(false)
const draft = ref({ name: '', platform: '' })
//: 左栏选中的那家店记在全局里：切去别的页再回来，还停在刚才那家。每页各记各的
//: 就是「回来又要重找一遍」，多店铺时这一步每天要重复十几次。
const picked = app.noted('deliver.store', '')

const shown = computed(() =>
  app.stores.filter(
    (s) =>
      (!app.platform || s.platform === app.platform) &&
      (!app.storeId || s.id === app.storeId),
  ),
)

const groups = computed(() => {
  const by = new Map()
  for (const s of shown.value) {
    const key = s.platform || '(未分平台)'
    if (!by.has(key)) by.set(key, [])
    by.get(key).push(s)
  }
  return [...by].map(([platform, list]) => ({
    platform,
    name: app.platforms.find((p) => p.id === platform)?.name || platform,
    list,
  }))
})

const here = computed(() => shown.value.find((s) => s.id === picked.value) || null)

async function load() {
  loading.value = true
  try {
    const got = await Promise.all(
      shown.value.map((s) => api.store(s.id).catch(() => null)),
    )
    detail.value = Object.fromEntries(shown.value.map((s, i) => [s.id, got[i]]))
    if (!shown.value.some((s) => s.id === picked.value)) {
      picked.value = shown.value[0]?.id || ''
    }
  } finally {
    loading.value = false
  }
}

watch(shown, load, { immediate: true })

/** 这家店在筛选中的账期上算到哪了。没选账期就看最新的。 */
function state(id) {
  const periods = detail.value[id]?.periods || []
  if (!periods.length) return null
  return app.period ? periods.find((p) => p.period === app.period) || null : periods[0]
}

function files(id) {
  return detail.value[id]?.files || []
}

function drop(storeId, name) {
  dialog.warning({
    title: '撤下这张表',
    content: `${name}。撤下后这家店会重算，损益表上的数会变。`,
    positiveText: '撤下',
    negativeText: '算了',
    onPositiveClick: async () => {
      try {
        await app.run('正在撤下并重算', () => api.dropFile(storeId, name))
        app.invalidate()
        await app.load(true)
        await load()
        message.success('撤下了')
      } catch (e) {
        message.error(e.message, { duration: 6000 })
      }
    },
  })
}

async function register() {
  if (!draft.value.name.trim() || !draft.value.platform) return
  // 店铺 id 是系统内部用的稳定标识，人不该被要求想一个。用平台加时间拼一个即可，
  // 界面上从头到尾只出现店名。
  const id = `${draft.value.platform}_${Date.now().toString(36)}`
  try {
    await api.addStore({
      id,
      name: draft.value.name.trim(),
      platform: draft.value.platform,
    })
    adding.value = false
    draft.value = { name: '', platform: '' }
    await app.load(true)
    picked.value = id
    message.success('登记好了。把这家店的表拖进来就能算账。')
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

function open(id) {
  const s = state(id)
  app.pick({ store: id })
  router.push({ name: 'period', params: { id }, query: { period: s?.period || '' } })
}
</script>

<template>
  <n-spin :show="loading">
    <div class="spread" style="margin-bottom: var(--s4)">
      <div>
        <h1>数据与店铺</h1>
        <div class="small muted">
          {{ count(shown.length) }} 家店。左边选一家，右边是它交过的表。
        </div>
      </div>
      <n-button size="small" @click="adding = true">登记新店</n-button>
    </div>

    <div class="cols list">
      <div class="card" style="margin-top: 0; padding: var(--s3)">
        <template v-for="g in groups" :key="g.platform">
          <div class="groupline">{{ g.name }} · {{ g.list.length }} 家</div>
          <button
            v-for="s in g.list"
            :key="s.id"
            class="pick"
            :class="{ on: s.id === picked }"
            @click="picked = s.id"
          >
            <div class="spread">
              <span class="who">{{ s.name }}</span>
              <span class="xs muted num">{{ files(s.id).length }} 张</span>
            </div>
            <div class="meta">
              <template v-if="state(s.id)">
                {{ state(s.id).period }} ·
                {{ state(s.id).state === 'closed' ? '已结账' : '未结账' }}
                <template v-if="state(s.id).profit !== undefined && state(s.id).profit !== null">
                  · 利润 <span class="num">{{ money(state(s.id).profit) }}</span>
                </template>
              </template>
              <template v-else>还没交过表</template>
            </div>
          </button>
        </template>
        <p v-if="!shown.length" class="xs muted" style="padding: var(--s3)">
          筛选条里没有匹配的店。
        </p>
      </div>

      <div v-if="here" class="card rail" style="margin-top: 0">
        <header>
          <h2>
            <button class="link" style="font-size: var(--t-lg); font-weight: 640" @click="open(here.id)">
              {{ here.name }}
            </button>
            <span v-if="here.archived" class="pill" style="margin-left: var(--s2)">已归档</span>
          </h2>
          <span class="sub">{{ count(files(here.id).length) }} 张表</span>
        </header>

        <div v-if="files(here.id).length" class="scroll tall">
          <n-table size="small" :bordered="false">
            <thead>
              <tr>
                <th>文件</th>
                <th>交表人</th>
                <th class="right">大小</th>
                <th class="right">更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="f in files(here.id)" :key="f.name">
                <td class="xs">
                  {{ f.name }}
                  <n-tag v-if="f.versions > 1" size="tiny" :bordered="false">
                    {{ f.versions }} 版
                  </n-tag>
                </td>
                <td class="xs muted">{{ f.by || '—' }}</td>
                <td class="right xs num">{{ bytes(f.size / 1024) }}</td>
                <td class="right xs muted nowrap">{{ ago(f.updated_at) || '—' }}</td>
                <td class="right">
                  <n-button size="tiny" quaternary type="error" @click="drop(here.id, f.name)">
                    撤下
                  </n-button>
                </td>
              </tr>
            </tbody>
          </n-table>
        </div>
        <DropZone v-else />

        <div v-if="(detail[here.id]?.periods || []).length" class="panel">
          <h3>算过的账期</h3>
          <div class="row wrap" style="margin-top: var(--s2)">
            <n-button
              v-for="p in detail[here.id].periods"
              :key="p.period"
              size="tiny"
              @click="open(here.id)"
            >
              {{ p.period }}
              <span class="xs muted" style="margin-left: 4px">
                {{ p.state === 'closed' ? '已结' : '未结' }}
              </span>
            </n-button>
          </div>
        </div>
      </div>
    </div>

    <n-modal
      v-model:show="adding"
      preset="dialog"
      title="登记新店"
      positive-text="登记"
      negative-text="算了"
      :positive-button-props="{ disabled: !draft.name.trim() || !draft.platform }"
      @positive-click="register"
    >
      <p class="small muted" style="margin-bottom: var(--s3)">
        只要店名和平台。账期从文件名认，主体和税号等到要开票时再说。
      </p>
      <n-space vertical>
        <n-input v-model:value="draft.name" placeholder="店铺名称，比如 淘宝喜必顺" />
        <n-select
          v-model:value="draft.platform"
          placeholder="选平台"
          :options="app.platforms.map((p) => ({ label: p.name, value: p.id }))"
        />
      </n-space>
    </n-modal>
  </n-spin>
</template>
