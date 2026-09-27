<template>
  <main class="auth-layout">
    <aside class="auth-brand-panel">
      <div class="auth-brand-lockup auth-brand-main-lockup">
        <div class="auth-brand-icon">
          <el-icon><OfficeBuilding /></el-icon>
        </div>
        <div>
          <div class="auth-brand-name">LAB RESERVE</div>
          <div class="auth-brand-caption">智能实验室预约平台</div>
        </div>
      </div>
      <div class="auth-brand-copy">
        <div class="auth-brand-eyebrow">CAMPUS LAB · RESERVATION</div>
        <h1 class="auth-brand-headline">让每一次实验，<br /><span>有备而来。</span></h1>
        <p class="auth-brand-description">
          连接实验室资源与日常教学，让空间、设备和预约安排都清晰有序。
        </p>
        <div class="auth-brand-index" aria-hidden="true">
          <span>01</span><span>02</span><span>03</span>
        </div>
      </div>
      <div class="auth-brand-foot">CAMPUS RESOURCE WORKSPACE</div>
    </aside>

    <section class="auth-main-panel">
      <div class="auth-form-wrap">
        <div class="auth-brand-mobile">
          <div class="auth-brand-icon">
            <el-icon><OfficeBuilding /></el-icon>
          </div>
          <div>
            <div class="auth-brand-name">LAB RESERVE</div>
            <div class="auth-brand-caption">智能实验室预约平台</div>
          </div>
        </div>
        <div class="auth-form-eyebrow">ACCOUNT ACCESS</div>
        <h2 class="auth-form-title">欢迎回来</h2>
        <p class="auth-form-description">登录后继续使用实验室预约工作台。</p>

        <el-form
          ref="formRef"
          class="auth-form"
          :model="form"
          :rules="rules"
          label-width="0px"
          @keyup.enter="login"
        >
          <el-form-item prop="username">
            <el-input
              v-model="form.username"
              size="large"
              placeholder="请输入账号"
              :prefix-icon="User"
              autocomplete="username"
            />
          </el-form-item>
          <el-form-item prop="password">
            <el-input
              v-model="form.password"
              type="password"
              size="large"
              placeholder="请输入密码"
              :prefix-icon="Lock"
              autocomplete="current-password"
              show-password
            />
          </el-form-item>
          <el-button
            class="auth-submit"
            size="large"
            type="primary"
            :loading="loadingValue"
            @click="login"
          >
            登录工作台
          </el-button>
          <div class="auth-switch">
            还没有账号？<router-link to="/register">创建账号</router-link>
          </div>
        </el-form>
      </div>
    </section>
  </main>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { User, Lock, OfficeBuilding } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { loginApi } from '@/api/auth'
import { useUser } from '@/utils/user'

const { saveLoginData } = useUser()

const form = reactive({
  username: '',
  password: ''
})

const formRef = ref()

// 表单校验规则（登录页只校验是否填写，长度约束属于注册页）
const rules = {
  username: [{ required: true, message: '请输入账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

const loadingValue = ref(false)

const login = async () => {
  // 校验不通过直接中断，valid 为 false
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loadingValue.value = true
  try {
    const res = await loginApi(form)
    if (res.code === 200) {
      saveLoginData(res.data)
      ElMessage.success('登录成功')
      await router.push('/manager/home')
    }
  } finally {
    loadingValue.value = false
  }
}
</script>
