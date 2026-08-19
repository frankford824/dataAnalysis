/* 数字怎么写出来。
 *
 * 这套界面是给人对账用的，不是给人看一眼截图的。所以两条规矩不能破：
 *
 * 空和零长得不一样。零是结论（这个月确实没花这笔钱），破折号是「还不知道」。
 * 混在一起会让人拿着不完整的表去报账。
 *
 * 金额一律两位小数、千分位、等宽。少一位或者位数不齐，扫一列时发现不了
 * 数量级错了的那一行——而数量级错了正是最常见、后果最大的一种错。
 */

const CN = 'zh-CN'

export const MISSING = '—'

export function money(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return MISSING
  return Number(v).toLocaleString(CN, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function count(v) {
  if (v === null || v === undefined) return MISSING
  return Number(v).toLocaleString(CN, { maximumFractionDigits: 0 })
}

export function percent(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return MISSING
  return `${(Number(v) * 100).toFixed(digits)}%`
}

/** 展板上的紧凑写法。只用在标题和说明里，正文金额一律写全。 */
export function brief(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return MISSING
  const n = Number(v)
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(1)} 万`
  return n.toFixed(0)
}

/* 差额。逐月对比里「比上月」那一列。
 *
 * 正数要带 +。没有加号的话，一列里正负混着看，眼睛只能靠有没有减号来分，
 * 而减号和破折号（还不知道）在小字号下几乎一样。 */

export function signed(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return MISSING
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${money(Math.abs(v))}`
}

export function signedPct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return ''
  const n = Number(v) * 100
  return `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(digits)}%`
}

/** 负数标红的判断。0 和空都不算。 */
export function negative(v) {
  return typeof v === 'number' && v < 0
}

export function bytes(kb) {
  if (!kb && kb !== 0) return MISSING
  return kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.round(kb)} KB`
}

/** 「3 分钟前」。对账时人关心的是新旧，不是那一串完整时间戳。 */
export function ago(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const secs = Math.round((Date.now() - then) / 1000)
  if (secs < 60) return '刚刚'
  if (secs < 3600) return `${Math.floor(secs / 60)} 分钟前`
  if (secs < 86400) return `${Math.floor(secs / 3600)} 小时前`
  if (secs < 86400 * 30) return `${Math.floor(secs / 86400)} 天前`
  return new Date(then).toLocaleDateString(CN)
}

export function stamp(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(CN, { hour12: false })
}

/** 账期字符串（2026-05）转成时间选择器要的毫秒数，以及反过来。 */
export function periodToTs(period) {
  if (!period) return null
  const [y, m] = period.split('-').map(Number)
  if (!y || !m) return null
  return new Date(y, m - 1, 1).getTime()
}

export function tsToPeriod(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

/** 未归类标签里的 `biz_type=` 翻成中文。已经算过的账期快照仍是引擎内部格式。 */
export function prettyUnmatched(label) {
  const text = (label || '').trim()
  const prefix = '（业务描述为空）'
  if (!text.startsWith(prefix)) return text
  const rest = text.slice(prefix.length).trim()
  if (!rest) return '业务描述为空'
  const names = { subject: '业务描述', remark: '备注', biz_type: '业务类型' }
  const parts = []
  const leftover = []
  for (const token of rest.split(/\s+/)) {
    const at = token.indexOf('=')
    if (at > 0) {
      const key = token.slice(0, at)
      parts.push(`${names[key] || key}：${token.slice(at + 1)}`)
    } else if (token) {
      leftover.push(token)
    }
  }
  const shown = [...parts, ...leftover].join('；')
  return shown ? `业务描述为空 · ${shown}` : '业务描述为空'
}
