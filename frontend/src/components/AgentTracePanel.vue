<template>
  <aside class="trace-panel">
    <header class="trace-head">
      <div class="trace-title">
        <span class="trace-mark">
          <el-icon><Cpu /></el-icon>
        </span>
        <div>
          <strong>Agent 执行轨迹</strong>
          <span>{{ subtitle }}</span>
        </div>
      </div>
      <el-button link size="small" @click="collapsed = !collapsed">
        {{ collapsed ? '展开' : '收起' }}
      </el-button>
    </header>

    <div v-show="!collapsed" class="trace-body">
      <div v-if="!trace.items.length" class="trace-empty">
        发起一次对话，这里会逐步展示 Agent 的理解、规划、工具调用与校验过程。
      </div>

      <ol v-else class="trace-list">
        <li
          v-for="item in trace.items"
          :key="item.id"
          class="trace-item"
          :class="[`trace-item--${item.status}`, `trace-item--${item.kind}`]"
        >
          <span class="trace-icon">
            <el-icon v-if="item.status === 'running'" class="is-spin"><Loading /></el-icon>
            <el-icon v-else-if="item.status === 'failed'"><Close /></el-icon>
            <el-icon v-else-if="item.status === 'stopped'"><Minus /></el-icon>
            <el-icon v-else><Check /></el-icon>
          </span>
          <div class="trace-text">
            <div class="trace-label">
              <span>{{ item.label }}</span>
              <em v-if="item.kind === 'tool'">TOOL</em>
              <i v-if="item.duration">{{ item.duration }}ms</i>
            </div>
            <p v-if="item.reason" class="trace-reason">{{ item.reason }}</p>
            <p v-if="item.detail" class="trace-detail">{{ item.detail }}</p>
          </div>
        </li>
      </ol>

      <section v-if="trace.plan.length" class="trace-block">
        <div class="trace-block-title">
          任务计划
          <em>第 {{ trace.planRound + 1 }} 轮</em>
        </div>
        <div
          v-for="step in trace.plan"
          :key="`${trace.planRound}-${step.id}`"
          class="trace-plan-row"
        >
          <b>{{ step.id }}</b>
          <div>
            <strong>{{ step.label }}</strong>
            <p v-if="step.reason">{{ step.reason }}</p>
          </div>
        </div>
      </section>

      <section v-if="trace.analysis" class="trace-block">
        <div class="trace-block-title">需求理解</div>
        <div class="trace-kv">
          <span>允许下单</span>
          <b :class="trace.analysis.authorized ? 'kv-yes' : 'kv-no'">
            {{ trace.analysis.authorized ? '是' : '否' }}
          </b>
          <template v-for="(value, key) in trace.analysis.slots" :key="key">
            <span>{{ slotLabels[key] || key }}</span
            ><b>{{ value }}</b>
          </template>
        </div>
        <div v-if="trace.analysis.missing.length" class="trace-missing">
          缺失信息：{{ missingText }}
        </div>
      </section>

      <section v-if="trace.reflection" class="trace-block">
        <div class="trace-block-title">
          反思与校验
          <em>第 {{ trace.reflection.round + 1 }} 轮</em>
        </div>
        <span
          class="trace-verdict"
          :class="trace.reflection.verdict === 'replan' ? 'trace-verdict--replan' : ''"
        >
          {{ trace.reflection.verdict === 'replan' ? '需要重新规划' : '通过，可以收尾' }}
        </span>
        <p class="trace-reflect">{{ trace.reflection.text }}</p>
      </section>

      <section class="trace-block">
        <div class="trace-block-title">
          长期记忆
          <el-button link size="small" @click="emit('refresh-memory')">刷新</el-button>
        </div>
        <div v-if="!memories.length" class="trace-empty trace-empty--inline">
          暂无长期记忆，完成一次预约后系统会自动学习你的偏好。
        </div>
        <div v-for="item in memories" :key="item.id" class="trace-memory">
          <span>{{ item.content }}</span>
          <em v-if="item.hit_count">命中 {{ item.hit_count }}</em>
        </div>
      </section>

      <section v-if="tools.length" class="trace-block">
        <div class="trace-block-title">
          可用工具 <em>{{ tools.length }}</em>
        </div>
        <div class="trace-tools">
          <span v-for="tool in tools" :key="tool.name" :title="tool.description">{{
            tool.label
          }}</span>
        </div>
      </section>
    </div>
  </aside>
</template>

<script setup>
import { computed, ref } from 'vue'
import { SLOT_LABELS } from '@/utils/agentTrace'

const props = defineProps({
  trace: { type: Object, required: true },
  memories: { type: Array, default: () => [] },
  tools: { type: Array, default: () => [] }
})
const emit = defineEmits(['refresh-memory'])

const collapsed = ref(false)
const slotLabels = SLOT_LABELS

// 事件里带的是原始槽位名（lab_name / date …），那是前后端的内部契约；
// 面板是给人看的，统一翻成中文，认不出来的键原样显示。
const missingText = computed(() =>
  (props.trace.analysis?.missing || []).map((key) => slotLabels[key] || key).join('、')
)

const subtitle = computed(() => {
  const { running, toolCount, totalMs, items } = props.trace
  if (running) return '正在运行…'
  if (!items.length) return '等待任务'
  const parts = [`${items.length} 个节点`, `${toolCount} 次工具调用`]
  if (totalMs) parts.push(`${(totalMs / 1000).toFixed(1)}s`)
  return parts.join(' · ')
})
</script>

<style scoped>
.trace-panel {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--app-line);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 8px rgba(26, 54, 39, 0.035);
}

.trace-head {
  display: flex;
  min-height: 68px;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--app-line);
}

.trace-title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.trace-mark {
  display: grid;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  place-items: center;
  border-radius: 9px;
  color: #fff;
  background: var(--app-green-deep);
  font-size: 15px;
}

.trace-title strong {
  display: block;
  color: var(--app-ink);
  font-size: 13px;
}

.trace-title div > span {
  display: block;
  overflow: hidden;
  margin-top: 2px;
  color: var(--app-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-body {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px 20px;
}

.trace-empty {
  padding: 14px 12px;
  border: 1px dashed var(--app-line);
  border-radius: 10px;
  color: var(--app-muted);
  background: #fafcfa;
  font-size: 11px;
  line-height: 1.7;
}

.trace-empty--inline {
  padding: 9px 10px;
  border-style: dashed;
}

.trace-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.trace-item {
  position: relative;
  display: flex;
  gap: 9px;
  padding-bottom: 13px;
}

/* 竖向连线：用伪元素画，避免多一层 DOM 拖累长列表 */
.trace-item::before {
  position: absolute;
  top: 20px;
  bottom: 0;
  left: 8px;
  width: 1px;
  background: var(--app-line);
  content: '';
}

.trace-item:last-child {
  padding-bottom: 0;
}

.trace-item:last-child::before {
  display: none;
}

.trace-icon {
  display: grid;
  z-index: 1;
  width: 17px;
  height: 17px;
  flex: 0 0 17px;
  place-items: center;
  border: 1px solid var(--app-line);
  border-radius: 50%;
  color: #fff;
  background: #fff;
  font-size: 11px;
}

.trace-item--done .trace-icon {
  border-color: #cfe4d6;
  color: #fff;
  background: #57a477;
}

.trace-item--running .trace-icon {
  border-color: var(--app-green);
  color: var(--app-green);
}

.trace-item--failed .trace-icon {
  border-color: #e8c4c0;
  background: #cf7a70;
}

/* 刷新页面时被中断的节点：既不转圈（早就不跑了），也不打勾（确实没跑完） */
.trace-item--stopped .trace-icon {
  border-color: #d8dfd9;
  color: #96a49b;
  background: #fff;
}

.trace-item--tool .trace-icon {
  border-radius: 4px;
}

.trace-text {
  min-width: 0;
  flex: 1;
}

.trace-label {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  color: var(--app-ink);
  font-size: 12px;
  font-weight: 650;
}

.trace-item--running .trace-label {
  color: var(--app-green);
}

.trace-item--failed .trace-label {
  color: #a8524a;
}

.trace-item--stopped .trace-label {
  color: var(--app-muted);
}

.trace-label em {
  padding: 1px 5px;
  border-radius: 3px;
  color: #47755e;
  background: #eef5f0;
  font-size: 9px;
  font-style: normal;
  font-weight: 700;
  letter-spacing: 0.4px;
}

.trace-label i {
  color: #96a49b;
  font-size: 10px;
  font-style: normal;
  font-weight: 500;
}

.trace-reason,
.trace-detail {
  margin: 4px 0 0;
  color: #74867b;
  font-size: 11px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.trace-reason {
  color: #8a9a90;
  font-style: italic;
}

.trace-item--tool .trace-detail {
  padding: 6px 8px;
  border-radius: 6px;
  background: #f7faf8;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
}

.trace-block {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px dashed var(--app-line);
}

.trace-block-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;

  /* 兼容 <b>/<em> 混排，用 flex 让「刷新」按钮贴右 */
  margin-bottom: 9px;
  color: #48604f;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.3px;
}

.trace-block-title em {
  color: #96a49b;
  font-size: 10px;
  font-style: normal;
  font-weight: 500;
}

.trace-plan-row {
  display: flex;
  gap: 8px;
  padding: 7px 0;
}

.trace-plan-row + .trace-plan-row {
  border-top: 1px solid #f1f5f2;
}

.trace-plan-row > b {
  display: grid;
  width: 17px;
  height: 17px;
  flex: 0 0 17px;
  place-items: center;
  border-radius: 5px;
  color: var(--app-green);
  background: #edf5ef;
  font-size: 10px;
}

.trace-plan-row strong {
  color: var(--app-ink);
  font-size: 11px;
}

.trace-plan-row p {
  margin: 3px 0 0;
  color: var(--app-muted);
  font-size: 10px;
  line-height: 1.55;
}

.trace-kv {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: baseline;
  gap: 5px 10px;
  font-size: 11px;
}

.trace-kv span {
  color: var(--app-muted);
}

.trace-kv b {
  overflow: hidden;
  color: var(--app-ink);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-kv .kv-yes {
  color: #3f8a63;
}

.trace-kv .kv-no {
  color: #b07a3c;
}

.trace-missing {
  margin-top: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  color: #a1701f;
  background: #fdf6e6;
  font-size: 10px;
}

.trace-verdict {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  color: #3f8a63;
  background: #edf5ef;
  font-size: 10px;
  font-weight: 650;
}

.trace-verdict--replan {
  color: #a1701f;
  background: #fdf6e6;
}

.trace-reflect {
  margin: 8px 0 0;
  color: #5c6f64;
  font-size: 11px;
  line-height: 1.7;
}

.trace-memory {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 9px;
  border-radius: 7px;
  background: #f6faf7;
  font-size: 11px;
}

.trace-memory + .trace-memory {
  margin-top: 5px;
}

.trace-memory span {
  color: #3f5749;
  line-height: 1.55;
}

.trace-memory em {
  flex: 0 0 auto;
  color: #96a49b;
  font-size: 9px;
  font-style: normal;
}

.trace-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.trace-tools span {
  padding: 3px 7px;
  border: 1px solid #e3ece6;
  border-radius: 999px;
  color: #66806f;
  background: #fbfdfb;
  font-size: 10px;
}

.is-spin {
  animation: trace-spin 1s linear infinite;
}

@keyframes trace-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
