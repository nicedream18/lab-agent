import request from '@/utils/request'

// Agent 相关接口。注意 request 的全局 timeout 只有 5 秒，
// Agent 一次对话要跑多个节点（含多次模型调用），必须单独放宽，
// 否则前端的 axios 会先超时、把「正在运行」误报成「网络异常」。
const AGENT_TIMEOUT = 30000

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

/** 历史会话列表（最近 30 天，按最后活跃时间倒序） */
export function getAgentConversationsApi() {
  return request({
    url: '/api/agent/conversations',
    method: 'get',
    timeout: AGENT_TIMEOUT
  })
}

/** 取回某条历史会话的全部消息（选中它接着聊） */
export function getAgentConversationApi(conversationId) {
  return request({
    url: `/api/agent/conversations/${encodeURIComponent(conversationId)}`,
    method: 'get',
    timeout: AGENT_TIMEOUT
  })
}

/** 删除某条历史会话（连同消息） */
export function deleteAgentConversationApi(conversationId) {
  return request({
    url: `/api/agent/conversations/${encodeURIComponent(conversationId)}`,
    method: 'delete',
    timeout: AGENT_TIMEOUT
  })
}
