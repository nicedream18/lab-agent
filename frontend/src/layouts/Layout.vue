<template>
  <el-container class="app-shell">
    <el-aside width="256px" class="app-sidebar">
      <div class="brand-block">
        <div class="brand-mark" aria-hidden="true">
          <el-icon><OfficeBuilding /></el-icon>
        </div>
        <div class="brand-copy">
          <div class="brand-title">LAB RESERVE</div>
          <div class="brand-caption">实验室预约平台</div>
        </div>
      </div>

      <div class="nav-caption">工作台</div>
      <el-menu class="app-menu" :default-active="route.path" router @select="mobileNavOpen = false">
        <el-menu-item v-for="item in navigation" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-bottom">
        <div class="service-indicator"><span></span> 预约服务已连接</div>
        <div class="sidebar-version">SMART LAB · CAMPUS</div>
      </div>
    </el-aside>

    <el-container class="workspace-column">
      <el-header class="workspace-header">
        <div class="header-leading">
          <el-button
            class="mobile-menu-button"
            text
            :icon="Menu"
            aria-label="打开导航菜单"
            @click="mobileNavOpen = true"
          />
          <div class="header-context">
            <div class="header-eyebrow">实验室预约工作台</div>
            <h1 class="header-title">{{ currentPageTitle }}</h1>
          </div>
        </div>

        <div class="header-actions">
          <el-tag effect="plain" round class="role-badge">{{ roleLabel }}</el-tag>
          <el-dropdown trigger="click" @command="handleCommand">
            <button class="account-trigger" type="button">
              <span class="account-avatar">{{ userInitial }}</span>
              <span class="account-name">{{ userInfo?.name || userInfo?.username || '用户' }}</span>
              <el-icon class="account-chevron"><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">
                  <el-icon><User /></el-icon>
                  个人信息
                </el-dropdown-item>
                <el-dropdown-item command="password">
                  <el-icon><Key /></el-icon>
                  修改密码
                </el-dropdown-item>
                <el-dropdown-item divided command="logout">
                  <el-icon><SwitchButton /></el-icon>
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="workspace-main">
        <div class="page-container">
          <router-view v-slot="{ Component, route: viewRoute }">
            <transition name="page-enter" mode="out-in">
              <component :is="Component" :key="viewRoute.name" />
            </transition>
          </router-view>
        </div>
        <footer class="workspace-footer">
          <span>智能实验室预约系统</span>
          <span>Campus Lab Reservation</span>
        </footer>
      </el-main>
    </el-container>

    <el-drawer
      v-model="mobileNavOpen"
      direction="ltr"
      size="min(84vw, 300px)"
      :with-header="false"
      class="mobile-navigation-drawer"
    >
      <div class="mobile-drawer-brand">
        <div class="brand-mark" aria-hidden="true">
          <el-icon><OfficeBuilding /></el-icon>
        </div>
        <div>
          <div class="brand-title">LAB RESERVE</div>
          <div class="brand-caption">实验室预约平台</div>
        </div>
      </div>
      <div class="nav-caption">工作台</div>
      <el-menu class="app-menu" :default-active="route.path" router @select="mobileNavOpen = false">
        <el-menu-item v-for="item in navigation" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-drawer>
  </el-container>
</template>

<script setup>
import { computed, ref } from 'vue'
import {
  ArrowDown,
  ChatDotRound,
  DocumentChecked,
  House,
  Menu,
  OfficeBuilding,
  Setting,
  SwitchButton,
  Tickets,
  User
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { logout } from '@/utils/auth'
import { useUser } from '@/utils/user'

const route = useRoute()
const router = useRouter()
const { userInfo } = useUser()
const mobileNavOpen = ref(false)

const roleLabel = computed(() => (userInfo.value?.role === 'admin' ? '管理员' : '学生'))
const userInitial = computed(() => (userInfo.value?.name || userInfo.value?.username || '用')[0])

const navigation = computed(() => {
  const shared = [
    { label: '系统首页', path: '/manager/home', icon: House },
    { label: 'AI 智能助手', path: '/manager/ai-chat', icon: ChatDotRound }
  ]

  if (userInfo.value?.role === 'admin') {
    return [
      shared[0],
      { label: '实验室管理', path: '/manager/lab', icon: OfficeBuilding },
      { label: '设备管理', path: '/manager/equipment', icon: Setting },
      { label: '用户管理', path: '/manager/user', icon: User },
      { label: '预约审核', path: '/manager/audit-reservation', icon: DocumentChecked },
      shared[1]
    ]
  }

  return [
    shared[0],
    { label: '实验室列表', path: '/manager/lablist', icon: OfficeBuilding },
    { label: '我的预约', path: '/manager/my-reservation', icon: Tickets },
    shared[1]
  ]
})

const currentPageTitle = computed(() => {
  if (route.name === 'LabEquipment') return '实验室设备'
  if (route.name === 'Profile') return '个人信息'
  if (route.name === 'Password') return '修改密码'
  return navigation.value.find((item) => item.path === route.path)?.label || '个人设置'
})

const handleCommand = async (command) => {
  if (command === 'profile') {
    await router.push('/manager/profile')
  } else if (command === 'password') {
    await router.push('/manager/password')
  } else if (command === 'logout') {
    try {
      await ElMessageBox.confirm('确定退出当前账号吗？', '退出登录', {
        confirmButtonText: '退出登录',
        cancelButtonText: '继续使用',
        type: 'warning'
      })
      logout()
      ElMessage.success('已安全退出')
      await router.push('/login')
    } catch {
      // 用户取消时留在当前页面。
    }
  }
}
</script>

<style scoped>
.app-shell {
  min-height: 100vh;
  background: var(--app-canvas);
}

.app-sidebar {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding: 24px 16px 18px;
  overflow: hidden;
  color: #edf5ee;
  background: var(--app-green-deep);
}

.brand-block,
.mobile-drawer-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 8px;
}

.brand-mark {
  display: grid;
  flex: 0 0 42px;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 12px;
  color: var(--app-green-deep);
  background: var(--app-lime);
  font-size: 22px;
}

.brand-title {
  color: #f7fbf6;
  font-size: 13px;
  font-weight: 800;
  line-height: 1.2;
}

.brand-caption {
  margin-top: 5px;
  color: #b6cfc0;
  font-size: 12px;
}

.nav-caption {
  margin: 38px 12px 12px;
  color: #96b2a0;
  font-size: 11px;
  font-weight: 700;
}

.app-menu {
  flex: 1;
  min-height: 0;
  border-right: 0;
  background: transparent;
}

.app-menu :deep(.el-menu-item) {
  height: 46px;
  margin: 4px 0;
  border-radius: 8px;
  color: #c7d9ce;
  font-weight: 600;
}

.app-menu :deep(.el-menu-item .el-icon) {
  color: #9fbcaa;
  font-size: 18px;
}

.app-menu :deep(.el-menu-item:hover) {
  color: #fff;
  background: rgba(255, 255, 255, 0.09);
}

.app-menu :deep(.el-menu-item.is-active) {
  color: #173c2e;
  background: var(--app-lime);
}

.app-menu :deep(.el-menu-item.is-active .el-icon) {
  color: #173c2e;
}

.sidebar-bottom {
  padding: 16px 10px 2px;
  border-top: 1px solid rgba(225, 241, 229, 0.14);
}

.service-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #d8e8dc;
  font-size: 12px;
}

.service-indicator span {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--app-lime);
  box-shadow: 0 0 0 4px rgba(201, 237, 114, 0.12);
}

.sidebar-version {
  margin-top: 12px;
  color: #86a492;
  font-size: 9px;
  font-weight: 700;
}

.workspace-column {
  min-width: 0;
}

.workspace-header {
  position: sticky;
  z-index: 5;
  top: 0;
  display: flex;
  height: 78px;
  align-items: center;
  justify-content: space-between;
  padding: 0 32px;
  border-bottom: 1px solid var(--app-line);
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(10px);
}

.header-leading,
.header-actions {
  display: flex;
  align-items: center;
}

.header-leading {
  gap: 10px;
}

.header-eyebrow {
  margin-bottom: 4px;
  color: #829087;
  font-size: 11px;
  font-weight: 700;
}

.header-title {
  color: var(--app-ink);
  font-size: 19px;
  font-weight: 750;
}

.header-actions {
  gap: 16px;
}

.role-badge {
  color: var(--app-green);
  border-color: #d7e7dc;
  background: #f3f8f3;
  font-weight: 700;
}

.account-trigger {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 4px 2px;
  border: 0;
  color: var(--app-ink);
  background: transparent;
  cursor: pointer;
}

.account-avatar {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid #d7e7dc;
  border-radius: 50%;
  color: var(--app-green-deep);
  background: #eaf3ed;
  font-weight: 800;
}

.account-name {
  max-width: 140px;
  overflow: hidden;
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-chevron {
  color: #87958c;
  font-size: 12px;
}

.mobile-menu-button {
  display: none;
}

.workspace-main {
  display: flex;
  min-height: calc(100vh - 78px);
  flex-direction: column;
  padding: 28px 32px 18px;
}

.page-container {
  width: 100%;
  max-width: 1440px;
  flex: 1;
  margin: 0 auto;
}

.workspace-footer {
  display: flex;
  justify-content: space-between;
  max-width: 1440px;
  width: 100%;
  margin: 28px auto 0;
  padding-top: 14px;
  border-top: 1px solid rgba(31, 70, 50, 0.09);
  color: #829087;
  font-size: 11px;
}

.page-enter-enter-active,
.page-enter-leave-active {
  transition:
    opacity 140ms ease,
    transform 140ms ease;
}

.page-enter-enter-from {
  opacity: 0;
  transform: translateY(5px);
}

.page-enter-leave-to {
  opacity: 0;
}

.mobile-drawer-brand {
  padding: 24px 20px 0;
}

.mobile-navigation-drawer :deep(.el-drawer__body) {
  padding: 0;
  color: #edf5ee;
  background: var(--app-green-deep);
}

.mobile-navigation-drawer .nav-caption {
  margin-top: 32px;
}

@media (max-width: 900px) {
  .app-sidebar {
    display: none;
  }

  .mobile-menu-button {
    display: inline-flex;
    color: var(--app-green-deep);
    font-size: 20px;
  }
}

@media (max-width: 640px) {
  .workspace-header {
    height: 68px;
    padding: 0 14px;
  }

  .header-eyebrow {
    font-size: 10px;
  }

  .header-title {
    font-size: 16px;
  }

  .header-actions {
    gap: 8px;
  }

  .role-badge {
    padding: 0 7px;
    font-size: 11px;
  }

  .account-name,
  .account-chevron {
    display: none;
  }

  .workspace-main {
    min-height: calc(100vh - 68px);
    padding: 16px 12px 14px;
  }

  .workspace-footer {
    gap: 8px;
    margin-top: 22px;
    font-size: 10px;
  }
}
</style>
