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
import { computed, ref, watch } from 'vue'

import { api } from './api'

//: 刷新之后还该记得的东西存这儿。只存「人选到哪儿了」，不存数据本身——数据要
//: 是也缓存下来，账重算过之后界面会拿旧数骗人。
const MEMO_KEY = 'ledger.memo'

function recall() {
  try {
    return JSON.parse(localStorage.getItem(MEMO_KEY) || '{}')
  } catch {
    return {}
  }
}

export const useApp = defineStore('app', () => {
  const boot = ref(null)
  const overview = ref(null)
  const loading = ref(false)
  const error = ref('')

  //: 上次看到哪：筛选条、各页选中的标签、左栏选中的店。刷新和切页都不该丢。
  const memo = ref(recall())
  watch(memo, (m) => localStorage.setItem(MEMO_KEY, JSON.stringify(m)), { deep: true })

  // 筛选条。空字符串一律表示「不限」。
  const platform = ref(memo.value.platform || '')
  const storeId = ref(memo.value.storeId || '')
  const period = ref(memo.value.period || '')
  watch([platform, storeId, period], ([p, s, t]) => {
    memo.value = { ...memo.value, platform: p, storeId: s, period: t }
  })

  //: 上传/重算这类要等的活。文案 + 起始时间，界面照着它显示秒数。
  const busy = ref(null)
  //: 上一次交表的结果。被拒的文件、没认出来的表都在这里，跳页之后还要能看见。
  const intake = ref(null)

  const YM = /^\d{4}-\d{2}$/

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
      // 默认账期必须是「多数店都有数的那个月」。空、以及认不出月份的「(未知账期)」，
      // 都不能拿来当默认值：后者排序在列表末尾，取最后一个就会让总览只剩一家店。
      const allowed = new Set(o.periods || [])
      if (!YM.test(period.value) || (allowed.size && !allowed.has(period.value))) {
        period.value = o.default_period || ''
      }
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

  /** 交表。
   *
   * 分两段等：文件传上去（能报字节百分比），服务端解析加重算（只能问服务端干到
   * 哪一步了）。第二段是大头——淘宝一个月的表十几秒起步，不报的话人看着一个转圈
   * 会以为死了，然后刷新，而刷新之后这次交表的结果就再也看不见了。
   */
  async function upload(files) {
    const list = [...files]
    if (!list.length) return null
    const what = list.length === 1 ? list[0].name : `${list.length} 个文件`
    const token = `u${Date.now()}${Math.random().toString(36).slice(2, 8)}`
    let poll = null
    busy.value = { label: `正在收 ${what}`, since: Date.now(), phase: '正在传', percent: 0 }
    try {
      const res = await api.upload(list, {
        token,
        onSent(loaded, total) {
          if (!busy.value) return
          busy.value.percent = Math.round((loaded / total) * 100)
          // 传完了就交给服务端，这时候才开始问它解析到哪儿。早问只会问到空。
          if (loaded >= total && !poll) {
            busy.value.phase = '收到了，正在解析'
            busy.value.percent = null
            poll = setInterval(async () => {
              try {
                const p = await api.uploadProgress(token)
                if (busy.value && p?.phase && !p.finished) {
                  busy.value.phase = p.total > 1 ? `${p.phase} ${p.done}/${p.total}` : p.phase
                }
              } catch {
                // 问不到就不显示，别把一次轮询失败弹成上传失败。
              }
            }, 600)
          }
        },
      })
      intake.value = res
      invalidate()
      return res
    } finally {
      clearInterval(poll)
      busy.value = null
    }
  }

  //: 交表结果面板开着没有。上一版只弹一句「收下 3 份表」就没了，被拒的文件、
  //: 认不出的表、算出来的账期全在响应里躺着，界面一个字都不显示。
  const showIntake = ref(false)

  /** 交表。所有入口都走这里：侧栏按钮、拖到窗口里、页面上的框。
   *
   * 不自动跳到最后一家店：人正开着某一页交表，被甩到别处是最讨厌的一种「帮忙」。
   * 算出来的账期在结果面板里列着，想去点一下就行。
   */
  async function submit(files) {
    const res = await upload(files)
    if (!res) return null
    await load(true)
    showIntake.value = true
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

  /** 记住页面里那些「上次翻到哪」的小选择：标签页、左栏选中的店。
   *
   * 组件自己 ref 的话，切走再回来就回到默认标签——人刚才在看的东西没了，还得
   * 重新点一遍。存这里就跟着 localStorage 一起活过刷新。
   */
  function noted(key, fallback = '') {
    return computed({
      get: () => memo.value[key] ?? fallback,
      set: (v) => (memo.value = { ...memo.value, [key]: v }),
    })
  }

  return {
    boot, overview, loading, error,
    platform, storeId, period, busy, intake, memo, showIntake,
    stores, platforms, visibleStores, periods, currentStore,
    load, invalidate, run, upload, submit, pick, noted,
  }
})
