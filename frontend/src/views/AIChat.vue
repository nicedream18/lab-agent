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
        <el-tag effect="plain" type="success" size="small">流式对话</el-tag>
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
            <div v-if="item.role === 'assistant' && item.steps?.length" class="message-steps">
              <div v-for="(step, stepIndex) in item.steps" :key="stepIndex">
                <span></span>{{ step }}
              </div>
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
  </div>
</template>

<script setup>
import { chatStreamApi } from '@/api/ai'
import { ref, reactive, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify' // 过滤危险的 HTML，防止 XSS 攻击

marked.setOptions({ breaks: true }) // 把 \n 转换成 <br>

// 最多携带的历史轮数（含当前这条），避免请求体无限增长
const MAX_HISTORY = 20

const messages = ref([
  {
    role: 'assistant',
    content:
      '你好，我是实验室预约助手。可以问规则和开放实验室，也可以说「帮我预约明天下午的实验室」，确认后我会帮你提交。'
  }
])
const input = ref('')
const loading = ref(false)
const listRef = ref()
const suggestedPrompts = [
  '现在有哪些开放的实验室？',
  '预约实验室需要遵守哪些规则？',
  '物理实验室有哪些设备？'
]

const scrollToBottom = () => {
  nextTick(() => {
    const elem = listRef.value
    if (elem) elem.scrollTop = elem.scrollHeight
  })
}

const handleSend = async () => {
  const text = input.value.trim()
  if (!text || loading.value) return
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  loading.value = true
  scrollToBottom()

  const assistant = reactive({
    role: 'assistant',
    content: '',
    status: '正在思考…',
    steps: []
  })
  messages.value.push(assistant)

  const history = messages.value
    .filter((item) => item.role === 'user' || item.role === 'assistant')
    .slice(0, -1)
    .slice(-MAX_HISTORY)
    .map(({ role, content }) => ({ role, content }))

  try {
    await chatStreamApi({ messages: history }, (evt) => {
      if (evt.type === 'status') {
        assistant.status = evt.message || '正在思考…'
      } else if (evt.type === 'tool_start') {
        const label = evt.label || evt.name || '工具'
        assistant.status = `正在${label}…`
        assistant.steps.push(`开始：${label}`)
      } else if (evt.type === 'tool_end') {
        const label = evt.label || evt.name || '工具'
        assistant.status = `${label}完成`
        assistant.steps.push(`完成：${label}`)
      } else if (evt.type === 'token') {
        assistant.content += evt.content || ''
        assistant.status = ''
      } else if (evt.type === 'done') {
        assistant.status = ''
      } else if (evt.type === 'error') {
        assistant.status = ''
        if (!assistant.content) assistant.content = evt.message || '请求失败'
        ElMessage.error(evt.message || '请求失败')
      }
      scrollToBottom()
    })
  } catch (error) {
    assistant.status = ''
    if (!assistant.content) assistant.content = error.message || '网络异常'
    ElMessage.error(error.message || '网络异常')
  } finally {
    loading.value = false
    assistant.status = ''
    scrollToBottom()
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
.ai-page {
  display: flex;
  min-height: 0;
  flex-direction: column;
}

.ai-page-heading {
  align-items: center;
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
  min-height: 520px;
  flex: 1;
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
  height: clamp(340px, calc(100vh - 390px), 620px);
  min-height: 320px;
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

.message-steps {
  display: grid;
  gap: 5px;
  margin-bottom: 8px;
  color: #74867b;
  font-size: 11px;
}

.message-steps > div {
  display: flex;
  align-items: baseline;
  gap: 7px;
}

.message-steps span {
  width: 5px;
  height: 5px;
  flex: 0 0 5px;
  border-radius: 50%;
  background: #8fac98;
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

@media (max-width: 640px) {
  .conversation-panel {
    min-height: 0;
  }

  .conversation-toolbar {
    min-height: 60px;
    padding: 10px 13px;
  }

  .message-list {
    height: 52vh;
    min-height: 280px;
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
