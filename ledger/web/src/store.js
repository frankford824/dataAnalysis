/* 全局状态。
 *
 * 装三样东西：启动时拿到的模型信息（店铺、平台、报表骨架）、总览快照、以及那条
 * 横在所有页面上方的筛选条。
 *
 * 筛选条要放在这里而不是各页自己管：人从展板点进某家店、再切到数据交付，选中的
 * 店和账期必须还在。每页自己记的话，切一次页就得重选一次，多店铺的场景下这套
 * 界面就没法用——这正是上一版界面被退回来的原因。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from './api'

export const useApp = defineStore('app', () => {
  const boot = ref(null)
  const overview = ref(null)
  const loading = ref(false)
  const error = ref('')

  // 筛选条。空字符串一律表示「不限」。
  const platform = ref('')
  const storeId = ref('')
  const period = ref('')

  //: 上传/重算这类要等的活。文案 + 起始时间，界面照着它显示秒数。
  const busy = ref(null)
  //: 上一次交表的结果。被拒的文件、没认出来的表都在这里，跳页之后还要能看见。
  const intake = ref(null)

  const stores = computed(() => overview.value?.stores || boot.value?.stores || [])

  const platforms = computed(() => {
    const seen = new Map()
    for (const s of boot.value?.platforms || []) seen.set(s.id, s.name || s.id)
    return [...seen].map(([id, name]) => ({ id, name }))
  })

  /** 当前筛选下可见的店。平台选了就只留那个平台的。 */
  const visibleStores = computed(() =>
    stores.value.filter((s) => !platform.value || s.platform === platform.value),
  )

  /** 所有出现过的账期，新的在前。 */
  const periods = computed(() => {
    const all = new Set(overview.value?.periods || [])
    return [...all].sort().reverse()
  })

  async function load(force = false) {
    if (overview.value && !force) return overview.value
    loading.value = true
    error.value = ''
    try {
      const [b, o] = await Promise.all([
        boot.value ? Promise.resolve(boot.value) : api.bootstrap(),
        api.overview(),
      ])
      boot.value = b
      overview.value = o
      if (!period.value && o.periods?.length) period.value = o.periods[o.periods.length - 1]
      return o
    } catch (e) {
      error.value = e.message
      throw e
    } finally {
      loading.value = false
    }
  }

  /** 账上的数变了，总览缓存就不能再用。 */
  function invalidate() {
    overview.value = null
  }

  async function run(label, fn) {
    busy.value = { label, since: Date.now() }
    try {
      return await fn()
    } finally {
      busy.value = null
    }
  }

  async function upload(files) {
    const list = [...files]
    if (!list.length) return null
    const what = list.length === 1 ? list[0].name : `${list.length} 个文件`
    const res = await run(`正在收 ${what}`, () => api.upload(list))
    intake.value = res
    invalidate()
    return res
  }

  /** 选中的这家店。筛选条上没选店时是 null。 */
  const currentStore = computed(
    () => stores.value.find((s) => s.id === storeId.value) || null,
  )

  function pick({ platform: p, store: s, period: t }) {
    if (p !== undefined) platform.value = p || ''
    if (s !== undefined) storeId.value = s || ''
    if (t !== undefined) period.value = t || ''
    // 选了店就把平台跟着对上，否则筛选条会自相矛盾：平台写着抖音、店是淘宝的。
    if (s) {
      const store = stores.value.find((x) => x.id === s)
      if (store && platform.value && store.platform !== platform.value) {
        platform.value = store.platform
      }
    }
    // 平台换了以后原来选的店可能不在这个平台，清掉，不然筛出来是空的。
    if (p && storeId.value) {
      const store = stores.value.find((x) => x.id === storeId.value)
      if (store && store.platform !== p) storeId.value = ''
    }
  }

  return {
    boot, overview, loading, error,
    platform, storeId, period, busy, intake,
    stores, platforms, visibleStores, periods, currentStore,
    load, invalidate, run, upload, pick,
  }
})
