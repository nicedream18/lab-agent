/**
 * Agent 执行轨迹的纯逻辑层。
 *
 * 为什么不写在组件里：SSE 事件流有几处容易踩的坑需要一次性处理干净 ——
 * 1. node 事件是 start / end 成对出现的，不能变成清单里的两行；
 * 2. agent 节点每轮都会再出现一次（问模型 → 调工具 → 再问模型），
 *    而「工具开始调用」和「调用完毕」是错开发出来的，只能靠
 *    findRunning 找当前那条运行中的记录来收尾，不能记下标号；
 * 3. execute 这类管道节点不占清单格，它要把结论并进对应的工具行，
 *    避免清单里出现两三行语义重复的记录。
 * 视图层只负责渲染，这些判断全部收敛在这里。
 *
 * v3 新增了「任务计划」这一层：
 *   plan 事件用同一个 type 承载三种 status（created / revised / step_done）。
 *   它不像 node/step 那样一句话就能落定，而是要维护一份**可推进的**步骤表，
 *   所以单独存在 trace.plan 里，不混进 items（items 是流水账，plan 是台账）。
 */

// 只有这几个节点值得在清单里单独占一行。
// advance 刻意不在此列：它是纯管道（不调模型、不碰数据库），后端也不为它发
// node 事件，所以它本来就不会出现在 items 里。
export const MAJOR_NODES = ['analyze', 'plan', 'agent', 'replan', 'respond']

export const NODE_LABELS = {
  analyze: '理解用户需求',
  plan: '拆解任务计划',
  agent: '自主决策',
  execute: '执行工具',
  replan: '调整任务计划',
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
  get_today: '获取当前日期',
  query_user_reservations: '查询我的预约',
  cancel_reservation: '取消预约'
}

/**
 * 会改动数据的工具。
 *
 * 为什么要在前端再维护一份：界面上「读一个状态」和「删一条预约」如果长得一样，
 * 用户就没法一眼看出 Agent 到底动没动他的数据。后端有同一份清单
 * （app/agent/state.py 的 WRITE_TOOLS），这里必须跟着同步 ——
 * 新增写库工具时两边都要加，否则工具行会静悄悄地少了那个警示标记。
 */
export const WRITE_TOOLS = ['create_reservation', 'cancel_reservation']

export const isWriteTool = (name) => WRITE_TOOLS.includes(name)

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
    // 任务计划台账。快路径（模型判定 plan_needed=false）下永远是 null，
    // 面板也就不会多出「任务计划」那一块 —— 简单请求的界面与 v2 一模一样。
    plan: null,
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
  const item = {
    id: seq,
    kind,
    label,
    detail,
    status: 'running',
    duration: null,
    reason: '',
    write: false
  }
  trace.items.push(item)
  return item
}

function settle(trace, kind, label, { failed = false, detail = '', duration = null } = {}) {
  const item = findRunning(trace, kind, label)
  if (!item) return null
  item.status = failed ? 'failed' : 'done'
  if (detail) item.detail = detail
  if (typeof duration === 'number') item.duration = duration
  return item
}

function applyNodeEvent(trace, evt) {
  const isMajor = MAJOR_NODES.includes(evt.node)
  const label = NODE_LABELS[evt.node] || evt.label || evt.node

  if (!isMajor) {
    // 管道节点：不占清单一格，把它的结论并进当前正在跑的那一行。
    if (evt.status === 'start') return '正在执行工具…'
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
    item.write = isWriteTool(evt.tool)
    return `正在${label}…`
  }

  trace.toolCount += 1
  const settled = settle(trace, 'tool', label, {
    failed: evt.status === 'failed',
    detail: evt.detail || '',
    duration: evt.duration_ms
  })
  // settle 之后条目已经不再是 running，所以必须靠它的返回值来补写库标记。
  // 直接收到终态事件（没有 running 前奏）时 settled 为 null，
  // 那种情况下本来也没建过行，不需要补。
  if (settled) settled.write = isWriteTool(evt.tool)
  return ''
}

/**
 * 任务计划的三类事件。
 *
 * 为什么要靠 done 数组记录「哪几步走完了」而不是只留一个游标：
 * 「走到第几步」和「哪几步真的成了」是两件事 —— 失败的那一步也会被推过去。
 * done 只保存**当前计划的**前缀，所以 done.length 恰好就是当前应该跑的下标，
 * 数组下标可以直接当步骤编号用，不需要额外的编号映射。
 */
function applyPlanEvent(trace, evt) {
  const steps = Array.isArray(evt.steps) ? evt.steps : []

  if (evt.status === 'created') {
    trace.plan = {
      steps,
      done: [],
      revision: Number(evt.revision) || 0,
      reason: evt.reason || '',
      status: 'created'
    }
    return steps.length ? `已拆解为 ${steps.length} 步任务…` : ''
  }

  if (evt.status === 'revised') {
    // 重规划失败时后端会回一份空 steps（意思是「保持原计划」），
    // 这时候只更新修订次数和理由，绝不能把已有计划erase掉。
    if (!trace.plan) {
      trace.plan = { steps, done: [], revision: 0, reason: '', status: 'created' }
    }
    if (steps.length) trace.plan.steps = steps
    trace.plan.status = 'revised'
    trace.plan.revision = Number(evt.revision) || trace.plan.revision || 0
    if (evt.reason) trace.plan.reason = evt.reason
    // 只保留**已经成功走完**的前缀：失败的那一步已经被新步骤替换掉了，
    // 留着老的编号会让下面的步骤与 done 下标错位。
    const kept = Number(evt.cursor) || 0
    trace.plan.done = trace.plan.done.slice(0, kept)
    return `第 ${trace.plan.revision} 次调整任务计划…`
  }

  if (evt.status === 'step_done') {
    if (!trace.plan) return ''
    trace.plan.done.push({
      step: Number(evt.cursor) || trace.plan.done.length + 1,
      ok: evt.ok !== false,
      goal: evt.goal || ''
    })
    return ''
  }

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
      return applyPlanEvent(trace, evt)
    case 'step':
      return applyStepEvent(trace, evt)
    case 'reset':
      // 模型先说了句铺垫又去调工具：那段文字不是答案，聊天区已经抹掉了。
      // 轨迹面板不需要额外处理，认下来是为了别掉进 default 分支后被当成未知事件。
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
 * 从留档里恢复计划台账。
 *
 * 逐字段重建而不是直接 Object.assign(saved.plan)：留档是 JSON.parse 出来的
 * 任意对象，字段缺失 / 类型不对都不该让整个面板崩掉。steps 里的 hint_tools
 * 也要过一遍，因为它会被直接渲染。
 */
function restorePlanState(saved) {
  if (!saved || typeof saved !== 'object') return null
  const steps = (Array.isArray(saved.steps) ? saved.steps : [])
    .filter((step) => step && typeof step === 'object')
    .map((step, index) => ({
      step: Number(step.step) || index + 1,
      goal: step.goal || '',
      hint_tools: Array.isArray(step.hint_tools) ? step.hint_tools.slice() : [],
      depends_on: Array.isArray(step.depends_on) ? step.depends_on.slice() : []
    }))
  if (!steps.length) return null

  const done = (Array.isArray(saved.done) ? saved.done : [])
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      step: Number(item.step) || 0,
      ok: item.ok !== false,
      goal: item.goal || ''
    }))
    // 留档可能写在「上一步刚推完、还没写下一步」的中间态，
    // 多出来的记录会让下标与步骤错位，这里按 steps 长度截断。
    .slice(0, steps.length)

  return {
    steps,
    done,
    revision: Number(saved.revision) || 0,
    reason: saved.reason || '',
    status: saved.status === 'revised' ? 'revised' : 'created'
  }
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
      reason: item.reason || '',
      write: Boolean(item.write)
    }))

  // id 是 :key，恢复出来的条目已经占用了 1..n，自增游标必须跳过它们，
  // 否则下一轮新起的条目会和旧条目撞 id（Vue 复用错节点）。
  seq = Math.max(seq, ...trace.items.map((item) => item.id), 0)

  trace.analysis = saved.analysis || null
  trace.plan = restorePlanState(saved.plan)
  trace.totalMs = typeof saved.totalMs === 'number' ? saved.totalMs : null
  trace.toolCount = Number(saved.toolCount) || 0
  trace.running = false
  return trace
}
