/**
 * AI 对话的本地留档：刷新页面（F5）后聊天内容不消失。
 *
 * 为什么用 localStorage 而不是新加一张后端表：
 * 刷新其实什么都没丢 —— 会话 id 和执行轨迹早就落库了，
 * 丢的只有「聊天区正在显示的那几行字」，而它本来就只活在前端内存里。
 * 把这份内存快照留在浏览器本地、刷新后原样铺回来就够了，
 * 不必为它开表、加接口、再走一遍鉴权。
 *
 * 有三条底线必须守住：
 * 1. **按登录用户分桶** —— 同一台电脑换个账号登录，绝不能看到上一个人的对话；
 * 2. **读写都不许抛异常** —— localStorage 可能被禁用，也可能写满 5MB 配额。
 *    对话缓存是锦上添花，坏了就当没有，不能连累整个页面打不开；
 * 3. **读出来的东西一律当不可信** —— 用户手改、旧版本残留都可能让结构对不上，
 *    所以每条消息都要校验一遍再用，不能让一个脏字段把渲染搞崩。
 */

const PREFIX = 'lab-agent:ai-chat'

// 只留最近若干条，避免 localStorage 无限膨胀
// （20 条 ≈ 10 轮往返，够用了；真正的长历史在后端轨迹里）
const MAX_MESSAGES = 120

function scopeKey(userId) {
  const scope = userId === null || userId === undefined || userId === '' ? 'anonymous' : userId
  return `${PREFIX}:${scope}`
}

/** 过滤掉结构不对的消息和「一个字都还没吐出来」的助手气泡。 */
function normalizeMessages(raw) {
  if (!Array.isArray(raw)) return []

  const cleaned = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const role = item.role === 'user' || item.role === 'assistant' ? item.role : null
    if (!role) continue
    const content = typeof item.content === 'string' ? item.content : ''
    // 空内容的助手消息 = 这一轮还没开始回答就被刷新了，留着只是个空气泡
    if (!content.trim()) continue
    cleaned.push({ role, content })
  }
  return cleaned.slice(-MAX_MESSAGES)
}

/**
 * 读出这个用户的对话留档。
 * 没有留档、结构不对、localStorage 不可用 —— 一律返回 null，
 * 由调用方回落到一句欢迎语，看起来就像刚进页面。
 */
export function loadChat(userId) {
  try {
    const text = localStorage.getItem(scopeKey(userId))
    if (!text) return null

    const parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object') return null

    const messages = normalizeMessages(parsed.messages)
    if (!messages.length) return null

    return {
      conversationId: typeof parsed.conversationId === 'string' ? parsed.conversationId : '',
      messages,
      trace: parsed.trace && typeof parsed.trace === 'object' ? parsed.trace : null
    }
  } catch {
    // JSON 坏了或浏览器禁用了存储，都按「没有留档」处理
    return null
  }
}

/** 写入留档。超配额时清掉旧数据，尽量不打断正在进行的对话。 */
export function saveChat(userId, { conversationId, messages, trace } = {}) {
  const payload = {
    conversationId: conversationId || '',
    messages: normalizeMessages(messages),
    trace: trace || null
  }

  try {
    localStorage.setItem(scopeKey(userId), JSON.stringify(payload))
  } catch {
    try {
      localStorage.removeItem(scopeKey(userId))
    } catch {
      // 存储被禁用时连删除也会抛，忽略即可
    }
  }
}

/** 清空留档（重开一条新对话、或删掉当前会话时用）。 */
export function clearChat(userId) {
  try {
    localStorage.removeItem(scopeKey(userId))
  } catch {
    // 同上：缓存清理失败不该冒泡给界面
  }
}
