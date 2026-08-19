<script setup>
/* 一份「这里有什么不对」的清单。
 *
 * 之前这些话分散在四个标签页里：自检说结不了账、该交的表说少了两张、质量说覆盖率
 * 只有 73%、没进利润的钱说有 308 块挂不上。四处都对，合起来却没有一处回答那个
 * 真正的问题——这张损益表我该不该信。
 *
 * 所以这里只做一件事：把它们摆成一条一条，每条一句人话，能点的就点进去。
 * 排序在后端定好了（拦路的在前，同级按金额），前端不再自己排——两处排序迟早会不一样。
 */
import { computed } from 'vue'

import { money } from '../format'

const props = defineProps({
  gaps: { type: Array, default: () => [] },
  //: 点一条能不能落到具体的地方。落不了就不做成可点的，免得点了没反应。
  clickable: { type: Boolean, default: false },
})

const emit = defineEmits(['open'])

/** 空值项和异常值项分开摆。处置方式不同：一个是去补数据，一个是去查。 */
const groups = computed(() => {
  const empty = props.gaps.filter((g) => g.kind === 'missing' || g.kind === 'empty')
  const odd = props.gaps.filter((g) => !(g.kind === 'missing' || g.kind === 'empty'))
  return [
    { key: 'empty', name: '空值项', hint: '这几项没有数，要补表', list: empty },
    { key: 'odd', name: '异常值项', hint: '有数但数看着不对，要查', list: odd },
  ].filter((g) => g.list.length)
})

const TONE = { blocking: 'error', warn: 'warning', info: 'default' }

function tone(g) {
  return TONE[g.severity] || 'default'
}

function can(g) {
  return props.clickable && Boolean(g.node || g.metric)
}
</script>

<template>
  <div class="gaps">
    <div v-if="!gaps.length" class="ok">
      <span class="tick">✓</span> 没找到空值项和异常项
    </div>

    <section v-for="g in groups" :key="g.key">
      <div class="head">
        <span class="name">{{ g.name }}</span>
        <span class="n">{{ g.list.length }}</span>
        <span class="hint">{{ g.hint }}</span>
      </div>
      <div
        v-for="(item, i) in g.list"
        :key="`${item.kind}-${item.node || item.metric || item.source || i}`"
        class="gap"
        :class="[tone(item), { tap: can(item) }]"
        @click="can(item) && emit('open', item)"
      >
        <div class="line">
          <span class="title">{{ item.title }}</span>
          <span v-if="item.amount !== null && item.amount !== undefined" class="num amt">
            {{ money(item.amount) }}
          </span>
        </div>
        <div class="detail">{{ item.detail }}</div>
        <div v-if="can(item)" class="go">
          {{ item.node === '__sources__' ? '去看该交的表 →' : '点开看这些行 →' }}
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.gaps {
  display: flex;
  flex-direction: column;
  gap: var(--s3);
}

.ok {
  color: var(--n6);
  font-size: var(--t-sm);
  padding: var(--s3) 0;
}

.tick {
  color: var(--ok);
  font-weight: 640;
}

.head {
  display: flex;
  align-items: baseline;
  gap: var(--s2);
  margin-bottom: var(--s2);
}

.head .name {
  font-weight: 620;
  font-size: var(--t-sm);
}

.head .n {
  font-family: var(--num);
  font-variant-numeric: tabular-nums;
  color: var(--n6);
  font-size: var(--t-xs);
}

.head .hint {
  color: var(--n6);
  font-size: var(--t-xs);
}

.gap {
  border-left: 3px solid var(--n3);
  padding: var(--s1) 0 var(--s1) var(--s3);
  margin-bottom: var(--s2);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
}

.gap.error {
  border-left-color: var(--bad);
}

.gap.warning {
  border-left-color: var(--warn);
}

.gap.tap {
  cursor: pointer;
}

.gap.tap:hover {
  background: var(--n1);
}

.line {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--s3);
}

.title {
  font-size: var(--t-sm);
  font-weight: 560;
}

.amt {
  font-size: var(--t-sm);
  white-space: nowrap;
}

.detail {
  color: var(--n6);
  font-size: var(--t-xs);
  line-height: 1.55;
  margin-top: 2px;
}

.go {
  color: var(--accent);
  font-size: var(--t-mono);
  margin-top: 2px;
}
</style>
