import request from '@/utils/request'

// Agent 相关接口。注意 request 的全局 timeout 只有 5 秒，
// Agent 一次对话要跑多个节点（含多次模型调用），必须单独放宽，
// 否则前端的 axios 会先超时、把「正在运行」误报成「网络异常」。
const AGENT_TIMEOUT = 30000

/** 查询某次会话的完整执行轨迹（落库在 agent_trace 表） */
export function getAgentTraceApi(conversationId) {
  return request({
    url: `/api/agent/trace/${conversationId}`,
    method: 'get',
    timeout: AGENT_TIMEOUT
  })
}

/** 查询 Agent 当前挂载的全部工具（用于前端展示「工具箱」） */
export function getAgentToolsApi() {
  return request({
    url: '/api/agent/tools',
    method: 'get',
    timeout: AGENT_TIMEOUT
  })
}

/** 查询当前用户的长期记忆 */
export function getAgentMemoryApi() {
  return request({
    url: '/api/agent/memory',
    method: 'get',
    timeout: AGENT_TIMEOUT
  })
}

/** 手动写入一条长期记忆 */
export function addAgentMemoryApi(data) {
  return request({
    url: '/api/agent/memory',
    method: 'post',
    data,
    timeout: AGENT_TIMEOUT
  })
}

/** 删除一条长期记忆 */
export function removeAgentMemoryApi(memoryId) {
  return request({
    url: `/api/agent/memory/${memoryId}`,
    method: 'delete',
    timeout: AGENT_TIMEOUT
  })
}
