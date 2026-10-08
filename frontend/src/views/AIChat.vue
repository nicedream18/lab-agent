<template>
  <div class="workspace-page ai-page">
    <div class="page-heading ai-page-heading">
      <div>
        <div class="page-kicker">LAB ASSISTANT</div>
        <h2 class="page-title">AI 智能助手</h2>
        <p class="page-description">查询实验资源、预约规则与开放安排。</p>
      </div>
      <div class="ai-availability"><span></span> 智能服务已连接</div>
    </div>

    <section class="conversation-panel">
      <div class="conversation-toolbar">
        <div class="conversation-heading">
          <span class="assistant-mark"
            ><el-icon><ChatDotRound /></el-icon
          ></span>
          <div>
            <strong>实验室助手</strong>
            <span>支持实时查询与预约协助</span>
          </div>
        </div>
        <div class="conversation-tools">
          <el-tag effect="plain" type="success" size="small">流式对话</el-tag>
          <el-button
            link
            size="small"
            :disabled="loading || messages.length <= 1"
            @click="handleClear"
          >
            清空对话
          </el-button>
        </div>
      </div>

      <div ref="listRef" class="message-list" aria-live="polite">
        <div
          v-for="(item, index) in messages"
          :key="index"
          class="message-row"
          :class="`message-row--${item.role}`"
        >
          <span v-if="item.role === 'assistant'" class="message-avatar">
            <el-icon><ChatDotRound /></el-icon>
          </span>
          <div class="message-bubble" :class="`message-bubble--${item.role}`">
            <div v-if="item.role === 'assistant' && item.status" class="message-status">
              {{ item.status }}
            </div>
            <div class="message-content" v-html="parseMarkdown(item.content)"></div>
          </div>
        </div>
      </div>

      <div v-if="messages.length === 1 && !loading" class="suggested-prompts">
        <span>试试这样问</span>
        <el-button
          v-for="prompt in suggestedPrompts"
          :key="prompt"
          plain
          size="small"
          @click="sendPrompt(prompt)"
        >
          {{ prompt }}
        </el-button>
      </div>

      <div class="composer">
        <el-input
          v-model="input"
          class="composer-input"
          type="textarea"
          :rows="2"
          maxlength="2000"
          resize="none"
          placeholder="输入实验室、设备或预约相关问题…"
          @keydown.enter.exact.prevent="handleSend"
          @keydown.enter.shift.stop
        ></el-input>
        <el-button
          class="composer-send"
          type="primary"
          :icon="ArrowUp"
          :loading="loading"
          :disabled="!input.trim()"
          aria-label="发送消息"
          title="发送消息"
          @click="handleSend"
        />
      </div>
      <div class="composer-caption">回答由 AI 生成，请以系统展示的实验室信息与审核结果为准。</div>
    </section>

    <AgentTracePanel
      :trace="trace"
      :memories="memories"
      :tools="agentTools"
      @refresh-memory="loadMemory"
    />
  </div>
</template>

<script setup>
import { chatStreamApi } from '@/api/ai'
import { getAgentMemoryApi, getAgentToolsApi } from '@/api/agent'
import {
  applyAgentEvent,
  createTraceState,
  finishTrace,
  restoreTraceState
} from '@/utils/agentTrace'
import { clearChat, loadChat, saveChat } from '@/utils/chatCache'
import { getUserInfo } from '@/utils/auth'
import AgentTracePanel from '@/components/AgentTracePanel.vue'
import { ref, reactive, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
// 图标已经在 main.js 里全局注册，但 :icon="ArrowUp" 是 JS 表达式，
// 全局组件拿不到这个变量，必须显式 import，否则图标一直是 undefined。
import { ArrowUp } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify' // 过滤危险的 HTML，防止 XSS 攻击

marked.setOptions({ breaks: true }) // 把 \n 转换成 <br>

// 最多携带的历史轮数（含当前这条），避免请求体无限增长
const MAX_HISTORY = 20

const GREETING =
  '你好，我是实验室预约助手。我背后是一条 Agent 工作流：先理解你的需求，再拆解成任务、逐个调用业务工具，最后自我校验。右侧会实时展示我的“思考轨迹”。'

// 上一条提问还没得到回答就刷新了。不能就这么把问题悬在那儿 ——
// 用户会以为助手还在想，所以补一句说清楚发生了什么。
const INTERRUPT_HINT = '（上一条提问的回答被页面刷新打断了，重新发送一次就好。）'

// 每次都新建数组：直接复用同一个对象会在「清空对话」时被引用共享串味
const createMessages = () => [{ role: 'assistant', content: GREETING }]

const messages = ref(createMessages())
const input = ref('')
const loading = ref(false)
const listRef = ref()

// 会话 id：由后端下发，多轮对话靠它串成同一条会话，
// 右侧面板的「历史轨迹」和后端长期记忆都依赖它。
const conversationId = ref('')

// Agent 执行轨迹。事件折叠逻辑全部在 utils/agentTrace.js 里，
// 这里只负责「收到事件 → 更新状态」，保持视图层干净。
const trace = reactive(createTraceState())
const memories = ref([])
const agentTools = ref([])

const suggestedPrompts = [
  '帮我预约明天下午2点到5点的计算机实验室，如果没有空闲就推荐其他时间',
  '现在有哪些开放的实验室？',
  '预约实验室需要遵守哪些规则？'
]

// 对话留档按账号分桶 —— 同一台电脑换个账号登录，
// 不能把上一个人聊过的内容铺给下一个人看。
const currentUser = getUserInfo()
const cacheUserId = currentUser?.id ?? currentUser?.username ?? ''

const scrollToBottom = () => {
  nextTick(() => {
    const elem = listRef.value
    if (elem) elem.scrollTop = elem.scrollHeight
  })
}

// 长期记忆和工具清单都是「锦上添花」的辅助信息，
// 拉取失败不应该打扰用户，静默降级成空列表即可。
const loadMemory = async () => {
  try {
    const res = await getAgentMemoryApi()
    memories.value = res.data || []
  } catch {
    memories.value = []
  }
}

const loadTools = async () => {
  try {
    const res = await getAgentToolsApi()
    agentTools.value = res.data || []
  } catch {
    agentTools.value = []
  }
}

/**
 * 把上次的对话铺回来。
 *
 * 除了那几行字，还必须把 conversationId 一起接上：不然下一句话
 * 会被后端当成一条全新会话，多轮上下文和「上一轮这单已经下过了」
 * 这类判断全部断掉，表现出「助手突然失忆」。
 */
const restoreChat = () => {
  const saved = loadChat(cacheUserId)
  if (!saved) return
  messages.value = saved.messages
  conversationId.value = saved.conversationId
  if (saved.trace) Object.assign(trace, restoreTraceState(saved.trace))
  // 最后一条是用户的提问，说明回答没来得及写完就刷新了。
  // （已经补过提示语时最后一条是助手消息，不会重复追加）
  if (messages.value.at(-1)?.role === 'user') {
    messages.value.push({ role: 'assistant', content: INTERRUPT_HINT })
    persistChat()
  }
  scrollToBottom()
}

/**
 * 写留档。
 *
 * 注意存的是 messages.value 本身而不是只存已完成的轮次：
 * 一轮流式回答要跑十几秒，用户很可能就在这中间刷新 ——
 * 把用户那句提问和已经吐出来的半截答案存住，刷新后至少还看得到。
 */
const persistChat = () => {
  saveChat(cacheUserId, {
    conversationId: conversationId.value,
    messages: messages.value,
    trace
  })
}

// 页面正在卸载（用户刷新 / 关标签页）时置位。
// 卸载会把流式请求整个掐断，前端只会看到一个 network error ——
// 但那不是故障，是用户自己按了刷新。所以这种时候：
//   既不能把这句话写进对话（刷新回来会莫名其妙多一句报错），
//   也不能把运行中的节点标成红色失败（它们只是被打断了）。
let leaving = false

const handleBeforeUnload = () => {
  leaving = true
  persistChat()
}

const handleClear = () => {
  ElMessageBox.confirm('清空后本次对话记录将无法恢复，确定继续吗？', '清空对话', {
    type: 'warning',
    confirmButtonText: '清空',
    cancelButtonText: '取消'
  })
    .then(() => {
      messages.value = createMessages()
      conversationId.value = ''
      Object.assign(trace, createTraceState())
      clearChat(cacheUserId)
      ElMessage.success('已清空对话')
    })
    // 点「取消」会 reject，必须接住，否则是个 unhandled rejection
    .catch(() => {})
}

onMounted(() => {
  restoreChat()
  loadMemory()
  loadTools()
  // 关键：流式回答跑到一半时刷新，并不会走 handleSend 的 finally，
  // 只有 beforeunload 能把这半截内容存下来。
  window.addEventListener('beforeunload', handleBeforeUnload)
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', handleBeforeUnload)
  // 顺手存一次：切到别的菜单页再回来，对话也不会重头开始
  persistChat()
})

const handleSend = async () => {
  const text = input.value.trim()
  if (!text || loading.value) return
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  loading.value = true

  // 每一轮都是一次全新的工作流执行，轨迹必须清空，
  // 否则上一轮的结果会和本轮混在一起，看不出因果关系。
  Object.assign(trace, createTraceState())
  trace.running = true
  scrollToBottom()

  const assistant = reactive({ role: 'assistant', content: '', status: '正在思考…' })
  messages.value.push(assistant)
  // 提问一落地就存一次，这样「刷新后只剩自己的问题、没有答案」
  // 也比「整段对话凭空消失」好得多
  persistChat()

  const history = messages.value
    .filter((item) => item.role === 'user' || item.role === 'assistant')
    .slice(0, -1)
    .slice(-MAX_HISTORY)
    .map(({ role, content }) => ({ role, content }))

  const startedAt = Date.now()
  let failed = false

  try {
    await chatStreamApi(
      {
        messages: history,
        ...(conversationId.value ? { conversation_id: conversationId.value } : {})
      },
      (evt) => {
        // 先把事件折叠进轨迹，顺便拿到可能的运行提示文案
        const hint = applyAgentEvent(trace, evt)

        if (evt.type === 'session') {
          conversationId.value = evt.conversation_id || conversationId.value
        } else if (evt.type === 'token') {
          assistant.content += evt.content || ''
          assistant.status = ''
        } else if (evt.type === 'done') {
          assistant.status = ''
          if (!assistant.content) assistant.content = evt.answer || ''
        } else if (evt.type === 'error') {
          failed = true
          assistant.status = ''
          if (!assistant.content && !leaving) assistant.content = evt.message || '请求失败'
          if (!leaving) ElMessage.error(evt.message || '请求失败')
        } else if (evt.type === 'status') {
          assistant.status = evt.message || '正在思考…'
        } else if (hint) {
          assistant.status = hint
        }
        scrollToBottom()
      }
    )
  } catch (error) {
    failed = true
    assistant.status = ''
    // 刷新导致的请求中断不是故障，见 leaving 的说明
    if (!assistant.content && !leaving) assistant.content = error.message || '网络异常'
    if (!leaving) ElMessage.error(error.message || '网络异常')
  } finally {
    loading.value = false
    assistant.status = ''
    if (leaving) {
      // 节点保持 running 原样落档 —— 恢复时会显示成「中断」，
      // 而不是打一个红色的叉（那一轮并没有失败）
      persistChat()
      return
    }
    // 兜底收尾：某些异常路径不会发 node end 事件，
    // 不处理的话界面上会有一条永远转圈的记录。
    finishTrace(trace, { failed })
    trace.totalMs = Date.now() - startedAt
    // 预约成功后后端会自动学习偏好，刷新一次才能看到新记忆
    if (trace.toolCount) loadMemory()
    scrollToBottom()
    persistChat()
  }
}

const sendPrompt = (prompt) => {
  input.value = prompt
  handleSend()
}

// 将 markdown 字符串转换为安全的 HTML 字符串
const parseMarkdown = (text) => {
  if (!text) return ''
  // 使用 marked 解析，然后用 DOMPurify 清理
  const rawHtml = marked.parse(text)
  return DOMPurify.sanitize(rawHtml)
}
</script>

<style scoped>
/* 左侧对话 + 右侧 Agent 轨迹。
   直接改 .ai-page 的布局（而不是再包一层 wrapper），
   这样整棵子树的缩进都不用动，diff 也小得多。 */
.ai-page {
  display: grid;
  /* 两栏高度只由视口决定，跟消息条数、轨迹长度彻底解耦：
       186px = 顶栏 78 + 主区上内边距 28 + 页脚占用 76（28 外边距 + 30 高 + 18 内边距）
     这里必须用 vh 而不是 height: 100%，因为 .workspace-main 只设了 min-height，
     高度还是 auto，百分比高度解析不出确定值，会退回「内容高度」。
     高度确定后，内容行才是确定的 minmax(0, 1fr)：
       内容超出不会把面板撑高，而是各自内部滚动
       （.message-list / .trace-body 的 overflow-y: auto）。 */
  height: calc(100vh - 186px);
  /* 窗口特别矮时不再继续压扁，溢出交给整页滚动兜底 */
  min-height: 520px;
  grid-template-rows: auto minmax(0, 1fr);
  grid-template-columns: minmax(0, 1fr) 336px;
  gap: 14px;
}

.ai-page-heading {
  align-items: center;
  grid-column: 1 / -1;
}

.ai-availability {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #47755e;
  font-size: 12px;
  font-weight: 650;
}

.ai-availability span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #57a477;
}

.conversation-panel {
  display: flex;
  /* 高度交给网格行，自己绝不用内容高度参与撑高 */
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--app-line);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 8px rgba(26, 54, 39, 0.035);
}

.conversation-toolbar {
  display: flex;
  min-height: 68px;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--app-line);
}

.conversation-heading {
  display: flex;
  align-items: center;
  gap: 11px;
}

.conversation-tools {
  display: flex;
  align-items: center;
  gap: 10px;
}

.assistant-mark,
.message-avatar {
  display: grid;
  flex: 0 0 34px;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 10px;
  color: var(--app-green);
  background: #edf5ef;
  font-size: 17px;
}

.conversation-heading strong,
.conversation-heading span:last-child {
  display: block;
}

.conversation-heading strong {
  color: var(--app-ink);
  font-size: 13px;
}

.conversation-heading div > span {
  margin-top: 3px;
  color: var(--app-muted);
  font-size: 11px;
}

.message-list {
  /* 占满工具栏、推荐词、输入框之外的全部剩余高度。
     注意不能再给 height / min-height，否则消息一多就会反过来撑高面板，
     面板高度就不再固定了。 */
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  padding: 22px clamp(14px, 4vw, 42px);
  background: #fbfcfb;
  scroll-behavior: smooth;
}

.message-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 18px;
}

.message-row--user {
  justify-content: flex-end;
}

.message-avatar {
  width: 30px;
  height: 30px;
  flex-basis: 30px;
  border-radius: 50%;
}

.message-bubble {
  max-width: min(78%, 760px);
  padding: 12px 15px;
  border: 1px solid #e5ece7;
  border-radius: 4px 12px 12px;
  color: #33443a;
  background: #fff;
  line-height: 1.75;
  overflow-wrap: anywhere;
}

.message-bubble--user {
  border-color: var(--app-green);
  border-radius: 12px 4px 12px 12px;
  color: #fff;
  background: var(--app-green);
}

.message-status {
  margin-bottom: 6px;
  color: #74867b;
  font-size: 11px;
}

.message-content :deep(p) {
  margin: 0 0 8px;
}

.message-content :deep(p:last-child) {
  margin-bottom: 0;
}

.message-content :deep(ul),
.message-content :deep(ol) {
  margin: 7px 0 8px;
  padding-left: 22px;
  list-style: initial;
}

.message-bubble--user .message-content :deep(ul),
.message-bubble--user .message-content :deep(ol) {
  padding-left: 22px;
}

.message-content :deep(pre) {
  overflow-x: auto;
  padding: 12px;
  border-radius: 8px;
  background: #f2f6f3;
}

.suggested-prompts {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 0 22px 13px;
}

.suggested-prompts > span {
  margin-right: 3px;
  color: var(--app-muted);
  font-size: 11px;
}

.suggested-prompts .el-button {
  margin: 0;
  color: #42624e;
  border-color: #dce8df;
  background: #f8fbf8;
  font-size: 11px;
}

.composer {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 14px 20px 8px;
  border-top: 1px solid var(--app-line);
}

.composer-input :deep(.el-textarea__inner) {
  min-height: 54px !important;
  padding: 13px 14px;
  border: 1px solid #dfe8e2;
  box-shadow: none;
}

.composer-send {
  width: 42px;
  height: 42px;
  flex: 0 0 42px;
  font-size: 17px;
}

.composer-caption {
  padding: 0 20px 12px;
  color: #849087;
  font-size: 10px;
}

@media (max-width: 1120px) {
  /* 窄屏放不下两栏：轨迹面板移到对话下方。
     堆叠布局下两个面板各占一个固定行高，内部照旧滚动，
     整个 .ai-page 随内容变高、交给页面滚动。 */
  .ai-page {
    height: auto;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(420px, 58vh) minmax(360px, 52vh);
  }
}

@media (max-width: 640px) {
  .conversation-toolbar {
    min-height: 60px;
    padding: 10px 13px;
  }

  .message-list {
    padding: 15px 11px;
  }

  .message-bubble {
    max-width: 88%;
    padding: 10px 12px;
    font-size: 13px;
  }

  .suggested-prompts {
    padding: 0 12px 12px;
  }

  .composer {
    padding: 12px 12px 7px;
  }

  .composer-caption {
    padding: 0 12px 10px;
  }
}
</style>
