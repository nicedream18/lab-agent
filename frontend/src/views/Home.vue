<template>
  <div class="home-page">
    <el-card shadow="never" class="welcome-card">
      <div class="welcome-content">
        <div>
          <div class="welcome-title">
            你好，{{ userInfo?.name || userInfo?.username || '同学' }}
          </div>
          <div class="welcome-subtitle">欢迎使用智能实验室预约系统 · 当前身份：{{ roleLabel }}</div>
        </div>
        <el-button type="primary" @click="goTo('/manager/ai-chat')"> 打开 AI 助手 </el-button>
      </div>
    </el-card>

    <el-row :gutter="16" class="stats-row">
      <el-col v-for="item in statCards" :key="item.label" :xs="24" :sm="8">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">{{ item.label }}</div>
          <div class="stat-value">{{ item.value }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="section-card">
      <template #header>
        <span class="section-title">系统能做什么</span>
      </template>
      <el-row :gutter="16">
        <el-col v-for="item in features" :key="item.title" :xs="24" :sm="12" :md="8">
          <div class="feature-item">
            <div class="feature-title">{{ item.title }}</div>
            <div class="feature-description">{{ item.desc }}</div>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <el-card shadow="never" class="section-card">
      <template #header>
        <span class="section-title">快捷入口</span>
      </template>
      <div class="shortcut-list">
        <el-button v-for="item in shortcuts" :key="item.path" @click="goTo(item.path)">
          {{ item.label }}
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUser } from '@/utils/user'
import { getLabPageList } from '@/api/lab'
import { getReservationPageList } from '@/api/reservation'

const { userInfo } = useUser()
const router = useRouter()

const labTotal = ref('-')
const reservationTotal = ref('-')

const roleLabel = computed(() => (userInfo.value?.role === 'admin' ? '管理员' : '学生'))

const statCards = computed(() => [
  { label: '开放实验室', value: labTotal.value },
  {
    label: userInfo.value?.role === 'admin' ? '预约总数' : '我的预约',
    value: reservationTotal.value
  },
  { label: 'AI 助手', value: '已接入' }
])

const features = [
  {
    title: '业务预约',
    desc: '浏览实验室与设备、提交预约，并查看预约进度或处理审核。'
  },
  {
    title: '知识库问答',
    desc: '检索实验室规则、安全规范和开放时间等资料，让回答有据可依。'
  },
  {
    title: '智能 Agent',
    desc: '通过工具查询实验室与设备；流式展示处理过程，并在确认后提交预约。'
  }
]

const shortcuts = computed(() => {
  const items = [
    { label: 'AI 智能助手', path: '/manager/ai-chat' },
    { label: '实验室列表', path: '/manager/lablist' },
    { label: '我的预约', path: '/manager/my-reservation' }
  ]

  if (userInfo.value?.role === 'admin') {
    items.push(
      { label: '预约审核', path: '/manager/audit-reservation' },
      { label: '实验室管理', path: '/manager/lab' },
      { label: '设备管理', path: '/manager/equipment' },
      { label: '用户管理', path: '/manager/user' }
    )
  }

  return items
})

const goTo = (path) => router.push(path)

onMounted(async () => {
  const [labResult, reservationResult] = await Promise.allSettled([
    getLabPageList({ page: 1, page_size: 1, status: 1 }),
    getReservationPageList({ page: 1, page_size: 1 })
  ])

  if (labResult.status === 'fulfilled' && labResult.value.code === 200) {
    labTotal.value = labResult.value.data?.total ?? 0
  }
  if (reservationResult.status === 'fulfilled' && reservationResult.value.code === 200) {
    reservationTotal.value = reservationResult.value.data?.total ?? 0
  }
})
</script>

<style scoped>
.home-page {
  display: grid;
  gap: 16px;
}

.welcome-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.welcome-title {
  color: #303133;
  font-size: 22px;
  font-weight: 700;
}

.welcome-subtitle {
  margin-top: 8px;
  color: #909399;
  font-size: 14px;
}

.stats-row {
  margin-bottom: -16px;
}

.stat-card {
  margin-bottom: 16px;
}

.stat-label {
  color: #909399;
  font-size: 13px;
}

.stat-value {
  margin-top: 8px;
  color: #303133;
  font-size: 28px;
  font-weight: 700;
}

.section-title {
  color: #303133;
  font-weight: 600;
}

.feature-item {
  padding: 8px 4px 16px;
}

.feature-title {
  color: #303133;
  font-size: 16px;
  font-weight: 600;
}

.feature-description {
  margin-top: 8px;
  color: #606266;
  font-size: 13px;
  line-height: 1.7;
}

.shortcut-list {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
</style>
