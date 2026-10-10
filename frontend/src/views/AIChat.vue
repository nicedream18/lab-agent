<template>
  <div class="workspace-page ai-page" :class="{ 'ai-page--history': historyOpen }">
    <div class="page-heading ai-page-heading">
      <div>
        <div class="page-kicker">LAB ASSISTANT</div>
        <h2 class="page-title">AI 智能助手</h2>
        <p class="page-description">查询实验资源、预约规则与开放安排。</p>
      </div>
      <div class="ai-availability"><span></span> 智能服务已连接</div>
    </div>

    <aside v-if="historyOpen" class="history-panel">
      <div class="history-head">
        <strong>历史会话</strong>
        <span>最近 30 天</span>
      </div>
      <p v-if="historyLoading" class="history-note">正在加载…</p>
      <p v-else-if="!historyList.length" class="history-note">
        还没有历史会话。聊过的内容会保留一个月，方便你随时翻回来接着聊。
      </p>
      <ul v-else class="history-list">
        <li
          v-for="item in historyList"
          :key="item.conversation_id"
          class="history-item"
          :class="{ 'history-item--active': item.conversation_id === conversationId }"
        >
          <button type="button" class="history-item-main" @click="openConversation(item)">
            <span class="history-item-title" :title="item.title || '未命名会话'">
              {{ item.title || '未命名会话' }}
            </span>
            <span class="history-item-meta">
              {{ formatHistoryTime(item.last_time) }} · {{ item.message_count }} 条消息
            </span>
          </button>
          <el-button
            class="history-item-remove"
            link
            size="small"
            title="删除该会话"
            aria-label="删除该会话"
            @click.stop="removeConversation(item)"
          >
            <el-icon><Delete /></el-icon>
          </el-button>
        </li>
      </ul>
    </aside>

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
          <el-button link size="small" @click="historyOpen = !historyOpen">
            {{ historyOpen ? '收起历史' : '历史会话' }}
          </el-button>
          <el-tag effect="plain" type="success" size="small">流式对话</el-tag>
          <el-button
            link
            size="small"
            title="当前对话会留在「历史会话」里，随时可以回去接着聊"
            :disabled="loading || messages.length <= 1"
            @click="handleNewConversation"
          >
            新对话
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
import {
  deleteAgentConversationApi,
  getAgentConversationApi,
  getAgentConversationsApi,
  getAgentMemoryApi,
  getAgentToolsApi
} from '@/api/agent'
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
  '你好，我是实验室预约助手。我背后是一条 Agent 工作流：先理解你的需求，需要的时候会把任务拆解成几步，再由模型自己决定调用哪些业务工具。右侧会实时展示我的“思考轨迹”。'

// 上一条提问还没得到回答就刷新了。不能就这么把问题悬在那儿 ——
// 用户会以为助手还在想，所以补一句说清楚发生了什么。
const INTERRUPT_HINT = '（上一条提问的回答被页面刷新打断了，重新发送一次就好。）'

// 每次都新建数组：直接复用同一个对象会在重开新对话时被引用共享串味
const createMessages = () => [{ role: 'assistant', content: GREETING }]

const messages = ref(createMessages())
const input = ref('')
const loading = ref(false)
const listRef = ref()

// 会话 id：由后端下发，多轮对话靠它串成同一条会话，
// 右侧面板的「历史轨迹」和后端长期记忆都依赖它。
const conversationId = ref('')

// 历史会话侧栏（最近 30 天，后端保留）。
// 不再只靠 localStorage 存「最后一次」—— 那样一开新对话，
// 上一次聊过的内容就永久没了，而用户想要的恰恰是「翻回去接着聊」。
const historyOpen = ref(false)
const historyLoading = ref(false)
const historyList = ref([])

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
 * 拉取最近 30 天的历史会话列表（历史会话是辅助能力，失败就静默降级成空列表）。
 *
 * 返回值区分「真的没有会话」和「列表没拉到」：调用方要据此决定
 * 能不能下「这条已保存」的结论 —— 拉失败时什么都不知道，不能说成已保存。
 */
const loadHistory = async () => {
  historyLoading.value = true
  try {
    const res = await getAgentConversationsApi()
    historyList.value = Array.isArray(res.data) ? res.data : []
    return true
  } catch {
    historyList.value = []
    return false
  } finally {
    historyLoading.value = false
  }
}

const pad2 = (value) => String(value).padStart(2, '0')

/**
 * 历史列表里的时间：近两天给出时分，一周内给「几天前」，更早给日期。
 * 保留期只有一个月，所以年份只在跨年那条兜底分支里才出现。
 */
const formatHistoryTime = (value) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''

  const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  const startOfDay = (item) =>
    new Date(item.getFullYear(), item.getMonth(), item.getDate()).getTime()
  const days = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86400000)

  if (days <= 0) return `今天 ${clock}`
  if (days === 1) return `昨天 ${clock}`
  if (days < 7) return `${days} 天前`
  const monthDay = `${date.getMonth() + 1} 月 ${date.getDate()} 日`
  return date.getFullYear() === new Date().getFullYear()
    ? monthDay
    : `${date.getFullYear()} 年 ${monthDay}`
}

/**
 * 打开一条历史会话：把消息铺回对话区，并把 conversation_id 接上。
 *
 * 必须接上 conversation_id，不然下一句话会被后端当成一条**全新**会话，
 * 多轮上下文和「上一轮这单已经下过了」这类判断全部断掉 ——
 * 表现就是「我明明接着上次在说，助手却像第一次听见」。
 *
 * 刻意不恢复右侧执行轨迹：那块面板画的是**这一次**运行的实时进度，
 * 历史上的节点此刻并没有在跑，硬铺回来只会让人以为 Agent 又在干活。
 */
const openConversation = async (item) => {
  if (!item?.conversation_id) return
  if (loading.value) {
    ElMessage.warning('请等当前回答结束再切换会话')
    return
  }
  // 已经在这条会话里就不再重拉：点自己的高亮项不该把正在滚动的视图弹回顶部
  if (item.conversation_id === conversationId.value && messages.value.length > 1) return

  try {
    const res = await getAgentConversationApi(item.conversation_id)
    if (res.code !== 200) {
      // 多半是这条会话刚被清掉/过期了。错误提示拦截器已经弹过，这里只把列表刷准。
      loadHistory()
      return
    }

    const payload = res.data || {}
    const restored = (Array.isArray(payload.messages) ? payload.messages : [])
      .filter(
        (msg) =>
          msg &&
          (msg.role === 'user' || msg.role === 'assistant') &&
          String(msg.content || '').trim()
      )
      .map((msg) => ({ role: msg.role, content: msg.content }))

    if (!restored.length) {
      ElMessage.warning('这条会话没有可显示的内容')
      return
    }

    messages.value = restored
    conversationId.value = item.conversation_id
    Object.assign(trace, createTraceState())
    persistChat()
    scrollToBottom()
  } catch (error) {
    // 网络异常等硬错误：让用户知道这次点击没生效，而不是默默什么都不动
    ElMessage.error(error?.message || '打开历史会话失败')
  }
}

/**
 * 删除一条历史会话。
 *
 * 当前正在聊的这条要单独确认：它是用户眼前的内容，
 * 列表里手滑点掉一条旧会话无所谓，把正在进行的对话删掉则完全是另一回事。
 */
const removeConversation = async (item) => {
  if (!item?.conversation_id) return
  if (loading.value) {
    ElMessage.warning('请等当前回答结束再操作')
    return
  }

  const isCurrent = item.conversation_id === conversationId.value
  try {
    await ElMessageBox.confirm(
      isCurrent
        ? '这是你当前正在进行的对话，删除后无法恢复，确定吗？'
        : '删除后该会话的聊天记录将无法恢复，确定继续吗？',
      '删除会话',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消'
      }
    )
  } catch {
    // 点「取消」会 reject，必须接住，否则是个 unhandled rejection
    return
  }

  try {
    await deleteAgentConversationApi(item.conversation_id)
    ElMessage.success('已删除该会话')
    if (isCurrent) {
      // 后台的记录已经没了，本地这份留着只会「看着还在、其实已经没了」——
      // 下次刷新还是空的。所以一起清掉，并回到一条全新对话。
      resetConversation()
    }
    loadHistory()
  } catch {
    // 删除失败的具体原因由响应拦截器提示，这里不再重复弹一条
  }
}

/** 把界面重置成一条全新对话（本地留档一并清掉） */
const resetConversation = () => {
  messages.value = createMessages()
  conversationId.value = ''
  Object.assign(trace, createTraceState())
  clearChat(cacheUserId)
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

/**
 * 「新对话」：把当前这条**留在历史会话里**，然后开一条全新的。
 *
 * 这里刻意**不删**服务端记录。后端在每一轮答完时就已经落库了
 * （见 conversation_service.record_turn），所以「保存」这件事其实只是
 * 「别把它删掉 + 把列表刷准」。想删某一条，用列表里那个删除按钮。
 *
 * 刷新列表顺便回答一个更要紧的问题：**它到底在不在**。
 * 如果整轮都撞上限流/报错，后端一轮都没落，那这条压根不存在 ——
 * 这时候不能说「已保存」，那是在骗用户。
 */
const handleNewConversation = async () => {
  if (loading.value) {
    ElMessage.warning('请等当前回答结束再开新对话')
    return
  }

  const current = conversationId.value
  const hadContent = messages.value.length > 1

  resetConversation()
  if (!hadContent) return

  const loaded = await loadHistory()
  const listed =
    loaded && !!current && historyList.value.some((item) => item.conversation_id === current)

  // 让「保存到哪去了」看得见：面板收着的话把它展开
  historyOpen.value = true

  if (listed) {
    ElMessage.success('已保存到历史会话，开始新对话')
  } else if (loaded) {
    ElMessage.warning('已开始新对话，但这次对话没有产生可保存的记录')
  } else {
    // 列表都没拉到，存没存下来并不知道 —— 不下结论
    ElMessage.info('已开始新对话')
  }
}

onMounted(() => {
  restoreChat()
  loadMemory()
  loadTools()
  // 有历史就默认展开侧栏 —— 「能翻回上一次的对话」只有看见才会被用上。
  // 没历史就一直显示引导语（空列表本身也是一句说明）。
  loadHistory().then(() => {
    if (historyList.value.length) historyOpen.value = true
  })
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
        } else if (evt.type === 'reset') {
          // 模型先说了句铺垫又去调工具 —— 那段文字不是答案（它还没看到工具结果），
          // 后端会为这种情况发一条 reset，这里把已经显示出来的抹掉。
          assistant.content = ''
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
    // 这一轮刚写进服务端留档，侧栏要跟着更新（标题、条数、活跃时间都会变）。
    // 侧栏关着就不用白跑一趟。
    if (historyOpen.value) loadHistory()
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

/* 展开「历史会话」时的三栏：会话列表 | 对话 | 执行轨迹。
   只有列表宽度是固定值，对话列吃掉剩下的全部空间 —— 辅助面板不该把主角挤窄。
   用 --history 修饰类而不是直接改 .ai-page：列表收起时列数必须回到 2，
   否则被挤到第三行的轨迹面板会掉到对话下方去。

   注意这个三栏形态只在**很宽**的窗口下成立，见下面 1500px 的断点。
   这里不能用「窗口宽度够不够」去估算可用空间：左侧导航栏会吃掉 320px，
   1199px 的窗口留给内容的其实只有 879px，再切成三栏对话列就只剩 275px 了。 */
.ai-page--history {
  grid-template-columns: 240px minmax(0, 1fr) 336px;
}

.history-panel {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--app-line);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 8px rgba(26, 54, 39, 0.035);
}

.history-head {
  display: flex;
  min-height: 52px;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--app-line);
}

.history-head strong {
  color: var(--app-ink);
  font-size: 13px;
}

.history-head span {
  color: var(--app-muted);
  font-size: 11px;
}

.history-note {
  margin: 0;
  padding: 14px;
  color: var(--app-muted);
  font-size: 12px;
  line-height: 1.7;
}

.history-list {
  /* 列表自己滚动，只占网格行给的高度，绝不反过来把面板撑高 */
  min-height: 0;
  flex: 1;
  margin: 0;
  padding: 6px;
  overflow-y: auto;
  list-style: none;
}

.history-item {
  display: flex;
  align-items: center;
  gap: 2px;
  border-radius: 9px;
}

.history-item:hover {
  background: #f4f8f5;
}

.history-item--active {
  background: #edf5ef;
}

/* 用原生 button 承载「切换会话」这件事：Tab 能走进去、回车能触发，
   键盘可达不该是额外成本。div + @click 会让键盘用户彻底够不着。 */
.history-item-main {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 4px;
  padding: 9px 10px;
  border: 0;
  background: none;
  cursor: pointer;
  text-align: left;
}

.history-item-main:focus-visible {
  outline: 2px solid var(--app-green);
  outline-offset: -2px;
  border-radius: 9px;
}

.history-item-title {
  overflow: hidden;
  color: var(--app-ink);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-item-main:hover .history-item-title,
.history-item--active .history-item-title {
  color: var(--app-green);
}

.history-item-meta {
  color: var(--app-muted);
  font-size: 11px;
}

.history-item-remove {
  flex: 0 0 auto;
  margin-right: 6px;
  color: #b3bdb6;
}

.history-item-remove:hover {
  color: #d9534f;
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

@media (max-width: 1500px) {
  /* 三栏塞不下时的退路：列表横过来，占对话上方一整行。
     为什么不让它继续当一列：切成三栏后对话列只剩两三百像素，
     消息气泡一行放不下几个字，得不偿失。
     为什么是「整行」而不是又压成一条窄列：横排卡片每个 186px，
     标题还能看全，滑一下就能翻到一个月前的会话。

     grid-row 不用写：列表带了 grid-column: 1 / -1（占满一行），
     自动排布就会把它推到第 2 行，对话和轨迹顺着落到第 3 行。 */
  .ai-page--history {
    grid-template-columns: minmax(0, 1fr) 336px;
    grid-template-rows: auto auto minmax(0, 1fr);
    /* 横条会吃掉一百多像素，窗口又矮的时候对话区会被压扁到不可用，
       不如让整页滚动。 */
    min-height: 600px;
  }

  .history-panel {
    grid-column: 1 / -1;
  }

  .history-list {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .history-item {
    flex: 0 0 200px;
  }
}

@media (max-width: 1120px) {
  /* 窄屏放不下两栏：轨迹面板移到对话下方。
     堆叠布局下两个面板各占一个固定行高，内部照旧滚动，
     整个 .ai-page 随内容变高、交给页面滚动。 */
  .ai-page,
  .ai-page--history {
    height: auto;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(420px, 58vh) minmax(360px, 52vh);
  }

  .ai-page--history {
    /* 历史列表在单列堆叠下也占一整行，多出来的就是这一行。
       上面 1500px 断点设的 min-height 在这里反而会平白多出滚动条，撤掉。 */
    grid-template-rows:
      auto
      auto
      minmax(420px, 58vh)
      minmax(360px, 52vh);
    min-height: 0;
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
