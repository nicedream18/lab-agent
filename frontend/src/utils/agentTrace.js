/**
 * Agent 执行轨迹的纯逻辑层。
 *
 * 为什么不写在组件里：SSE 事件流有几处容易踩的坑需要一次性处理干净 ——
 * 1. node 事件是 start / end 成对出现的，不能变成清单里的两行；
 * 2. 「目标时段被占用」会触发重规划，plan / reflect 节点因此会**重复出现**，
 *    这时候必须新起一行，而不是把上一轮的结果覆盖掉；
 * 3. route / execute 这类管道节点，信息要并进对应的工具行，避免清单里
 *    出现两三行语义重复的记录。
 * 视图层只负责渲染，这些判断全部收敛在这里。
 */

// 只有这几个节点值得在清单里单独占一行。
export const MAJOR_NODES = ['analyze', 'plan', 'reflect', 'respond']

export const NODE_LABELS = {
  analyze: '理解用户需求',
  plan: '制定任务计划',
  route: '选择工具',
  execute: '执行工具',
  reflect: '反思与校验',
  respond: '生成最终回复'
}

// 工具名 → 展示文案。故意写得比后端更「业务化」，
// 因为 Trace Panel 是给用户和面试官看的，不是给工程师看代码的。
export const TOOL_LABELS = {
  query_lab_availability: '查询实验室状态',
  check_user_permission: '检查权限',
  create_reservation: '创建预约',
  verify_reservation: '验证结果',
  find_available_slots: '查找替代时段',
  list_open_labs: '查询开放实验室',
  list_lab_equipments: '查询实验室设备',
  search_lab_docs: '检索预约规则',
  get_today: '获取当前日期'
}

export const SLOT_LABELS = {
  lab_name: '实验室',
  equipment_name: '设备',
  date: '日期',
  start_time: '开始',
  end_time: '结束',
  duration_hours: '时长(h)',
  keywords: '关键词'
}

const isBlank = (value) =>
  value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)

/**
 * 创建一个新的轨迹状态。整轮对话用同一个对象，调用方在每次提问前重置。
 * 这里返回普通对象而非 reactive 包装，由调用方决定要不要放进 Vue 的响应式系统。
 */
export function createTraceState() {
  return {
    items: [],
    analysis: null,
    plan: [],
    planRound: 0,
    reflection: null,
    totalMs: null,
    toolCount: 0,
    running: false
  }
}

export function formatArgs(args) {
  if (!args || typeof args !== 'object') return ''
  return Object.entries(args)
    .filter(([, value]) => !isBlank(value))
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join('/') : value}`)
    .join('   ')
}

let seq = 0

function findRunning(trace, kind, label) {
  for (let i = trace.items.length - 1; i >= 0; i -= 1) {
    const item = trace.items[i]
    if (item.kind === kind && item.label === label && item.status === 'running') return item
  }
  return null
}

function begin(trace, kind, label, detail = '') {
  const running = findRunning(trace, kind, label)
  if (running) {
    if (detail) running.detail = detail
    return running
  }
  seq += 1
  const item = { id: seq, kind, label, detail, status: 'running', duration: null, reason: '' }
  trace.items.push(item)
  return item
}

function settle(trace, kind, label, { failed = false, detail = '', duration = null } = {}) {
  const item = findRunning(trace, kind, label)
  if (!item) return
  item.status = failed ? 'failed' : 'done'
  if (detail) item.detail = detail
  if (typeof duration === 'number') item.duration = duration
}

function applyNodeEvent(trace, evt) {
  const isMajor = MAJOR_NODES.includes(evt.node)
  const label = NODE_LABELS[evt.node] || evt.label || evt.node

  if (!isMajor) {
    // 管道节点：不占清单一格，把它的结论并进当前正在跑的那一行。
    if (evt.status === 'start') {
      return evt.node === 'route' ? '正在选择工具…' : '正在执行工具…'
    }
    const last = [...trace.items].reverse().find((item) => item.status === 'running')
    if (last && evt.detail && !last.detail) last.detail = evt.detail
    return ''
  }

  if (evt.status === 'start') {
    begin(trace, 'node', label)
    return `正在${label}…`
  }

  settle(trace, 'node', label, {
    failed: evt.state === 'failed',
    detail: evt.detail || '',
    duration: evt.duration_ms
  })
  return ''
}

function applyStepEvent(trace, evt) {
  const label = TOOL_LABELS[evt.tool] || evt.label || evt.tool || '工具'

  if (evt.status === 'running') {
    const item = begin(trace, 'tool', label, formatArgs(evt.args))
    item.reason = evt.reason || ''
    return `正在${label}…`
  }

  trace.toolCount += 1
  settle(trace, 'tool', label, {
    failed: evt.status === 'failed',
    detail: evt.detail || '',
    duration: evt.duration_ms
  })
  return ''
}

/**
 * 把一条 SSE 事件折叠进轨迹状态。
 * 返回一个可选的状态提示文案，调用方可用它覆盖聊天区的 loading 文案；
 * 返回空字符串表示这条事件不需要更新提示。
 */
export function applyAgentEvent(trace, evt) {
  switch (evt.type) {
    case 'node':
      return applyNodeEvent(trace, evt)
    case 'analysis':
      trace.analysis = {
        slots: evt.slots || {},
        missing: evt.missing_slots || [],
        authorized: Boolean(evt.authorized),
        detail: evt.detail || ''
      }
      return ''
    case 'plan':
      trace.planRound = evt.round || 0
      trace.plan = (evt.steps || []).map((step) => ({
        id: step.id,
        tool: step.tool,
        label: TOOL_LABELS[step.tool] || step.label || step.tool,
        reason: step.reason || ''
      }))
      return ''
    case 'step':
      return applyStepEvent(trace, evt)
    case 'reflection':
      trace.reflection = {
        round: evt.round || 0,
        verdict: evt.verdict || 'finish',
        text: evt.text || ''
      }
      return ''
    default:
      return ''
  }
}

/** 收尾：把还挂在「运行中」的条目按最终结果落定，避免界面一直转圈。 */
export function finishTrace(trace, { failed = false } = {}) {
  trace.items.forEach((item) => {
    if (item.status === 'running') item.status = failed ? 'failed' : 'done'
  })
  trace.running = false
}

/**
 * 从本地留档恢复上一轮的轨迹（刷新页面用）。
 *
 * 唯一需要修补的是「运行中」：留档可能是在一轮对话跑到一半时写下的，
 * 而那些条目再也不会有人来给它们收尾了。统一改成 stopped ——
 * 既不能继续转圈（假装还在跑），也不能标成 done（谎称跑完了）。
 */
export function restoreTraceState(saved) {
  const trace = createTraceState()
  if (!saved || typeof saved !== 'object') return trace

  trace.items = (Array.isArray(saved.items) ? saved.items : [])
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      id: Number(item.id) || 0,
      kind: item.kind || 'node',
      label: item.label || '',
      detail: item.detail || '',
      status: item.status === 'running' ? 'stopped' : item.status || 'done',
      duration: typeof item.duration === 'number' ? item.duration : null,
      reason: item.reason || ''
    }))

  // id 是 :key，恢复出来的条目已经占用了 1..n，自增游标必须跳过它们，
  // 否则下一轮新起的条目会和旧条目撞 id（Vue 复用错节点）。
  seq = Math.max(seq, ...trace.items.map((item) => item.id), 0)

  trace.analysis = saved.analysis || null
  trace.plan = Array.isArray(saved.plan) ? saved.plan : []
  trace.planRound = Number(saved.planRound) || 0
  trace.reflection = saved.reflection || null
  trace.totalMs = typeof saved.totalMs === 'number' ? saved.totalMs : null
  trace.toolCount = Number(saved.toolCount) || 0
  trace.running = false
  return trace
}
