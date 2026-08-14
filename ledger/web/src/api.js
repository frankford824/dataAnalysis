/* 后端调用。
 *
 * 出错时一定要把后端那句话原样带出来。FastAPI 的 detail 里写的是「这张表里没有
 * 数据行」「结不了：还缺三张表」这类人能照着做事的话；替换成「请求失败」等于把
 * 唯一有用的信息扔了。
 */

async function call(path, init) {
  let res
  try {
    res = await fetch(path, init)
  } catch {
    // 网络层失败没有 detail 可取。这套系统跑在内网，多半是服务没起来。
    throw new Error('连不上服务。它可能没在跑，或者这台机器不在内网里。')
  }
  const text = await res.text()
  let body = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  if (!res.ok) {
    throw new Error(body?.detail || text || `请求失败（${res.status}）`)
  }
  return body
}

function query(params) {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') q.set(k, v)
  }
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const api = {
  bootstrap: () => call('/api/bootstrap'),

  overview: () => call('/api/overview'),
  trend: (params) => call(`/api/trend${query(params)}`),
  gaps: (params) => call(`/api/gaps${query(params)}`),
  store: (id) => call(`/api/stores/${encodeURIComponent(id)}`),
  period: (id, period) =>
    call(`/api/stores/${encodeURIComponent(id)}/periods/${encodeURIComponent(period)}`),
  recompute: (id) =>
    call(`/api/stores/${encodeURIComponent(id)}/recompute`, { method: 'POST' }),
  close: (id, period, note = '') =>
    call(`/api/stores/${encodeURIComponent(id)}/periods/${encodeURIComponent(period)}/close`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ note }),
    }),
  reopen: (id, period, note) =>
    call(`/api/stores/${encodeURIComponent(id)}/periods/${encodeURIComponent(period)}/reopen`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ note }),
    }),

  /** 交表。
   *
   * 这里用 XHR 而不是 fetch，只为一件事：`upload.onprogress`。传一份两百兆的
   * 订单明细要几十秒，fetch 给不出已经传了多少，界面就只能转圈——而转圈证明不了
   * 它还活着。`token` 是这次交表的号，服务端拿它报解析到哪一步了。
   */
  upload: (files, { token = '', onSent } = {}) =>
    new Promise((resolve, reject) => {
      const form = new FormData()
      // 文件名要原样带上：引擎靠它认店铺和账期。
      for (const f of files) form.append('files', f, f.name)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', `/api/upload${query({ token })}`)
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onSent) onSent(e.loaded, e.total)
      }
      xhr.onload = () => {
        let body = null
        try {
          body = xhr.responseText ? JSON.parse(xhr.responseText) : null
        } catch {
          body = null
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body)
        else reject(new Error(body?.detail || xhr.responseText || `请求失败（${xhr.status}）`))
      }
      xhr.onerror = () =>
        reject(new Error('连不上服务。它可能没在跑，或者这台机器不在内网里。'))
      xhr.send(form)
    }),
  uploadProgress: (token) => call(`/api/upload/progress/${encodeURIComponent(token)}`),
  dropFile: (storeId, name) =>
    call(`/api/stores/${encodeURIComponent(storeId)}/files${query({ name })}`, {
      method: 'DELETE',
    }),

  search: (params) => call(`/api/search${query(params)}`),
  drill: (runId, nodeId, params) =>
    call(`/api/runs/${runId}/drill/${encodeURIComponent(nodeId)}${query(params)}`),

  stores: () => call('/api/stores'),
  patchStore: (id, patch) =>
    call(`/api/stores/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  addStore: (store) =>
    call('/api/stores', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(store),
    }),

  commission: (period) => call(`/api/commission${query({ period })}`),
  commissionConfig: (storeId) => call(`/api/commission/config${query({ store_id: storeId })}`),
  commissionProducts: (params) => call(`/api/commission/products${query(params)}`),
  commissionPlan: (plan, apply) =>
    call(`/api/commission/plan${query({ apply: apply ? 'true' : '' })}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(plan),
    }),
  uploadCommission: (file, recomputeStores) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return call(
      `/api/commission/config${query({ recompute_stores: recomputeStores ? 'true' : '' })}`,
      { method: 'POST', body: form },
    )
  },

  roles: (source) => call(`/api/roles${query({ source })}`),
  draft: (sha, params) => call(`/api/onboard/${sha}${query(params)}`),
  assist: (sha, params) => call(`/api/onboard/${sha}/assist${query(params)}`),
  onboardTry: (commit) =>
    call('/api/onboard/try', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(commit),
    }),
  onboard: (commit) =>
    call('/api/onboard', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(commit),
    }),
}
