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
        <div class="auth-brand-eyebrow">YOUR CAMPUS WORKSPACE</div>
        <h1 class="auth-brand-headline">从这里开始，<br /><span>探索更多可能。</span></h1>
        <p class="auth-brand-description">
          一个账号，即可浏览实验资源、提交预约，并与智能助手交流。
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
        <div class="auth-form-eyebrow">CREATE ACCOUNT</div>
        <h2 class="auth-form-title">创建账号</h2>
        <p class="auth-form-description">注册后即可进入校园实验室预约工作台。</p>

        <el-form ref="formRef" class="auth-form" :rules="rules" :model="form" label-width="0px">
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
              autocomplete="new-password"
              show-password
            />
          </el-form-item>
          <el-form-item prop="confirmPassword">
            <el-input
              v-model="form.confirmPassword"
              type="password"
              size="large"
              placeholder="请确认密码"
              :prefix-icon="Lock"
              autocomplete="new-password"
              show-password
            />
          </el-form-item>
          <el-button
            class="auth-submit"
            size="large"
            type="primary"
            :loading="loadingValue"
            @click="register"
          >
            创建账号
          </el-button>
          <div class="auth-switch">已有账号？<router-link to="/login">返回登录</router-link></div>
        </el-form>
      </div>
    </section>
  </main>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { User, Lock, OfficeBuilding } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { registerApi } from '@/api/auth'
import router from '@/router'

const form = reactive({
  username: '',
  password: '',
  confirmPassword: ''
})

const loadingValue = ref(false)

const formRef = ref()

const validatePass = (rule, value, callback) => {
  if (value === '') {
    callback(new Error('请确认密码'))
  } else {
    if (value !== form.password) {
      callback(new Error('两次密码输入不一致'))
    } else {
      callback()
    }
  }
}
const rules = {
  username: [{ required: true, message: '请输入账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
  confirmPassword: [{ validator: validatePass, trigger: 'blur' }]
}

const register = async () => {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  loadingValue.value = true
  try {
    const res = await registerApi(form)
    if (res.code === 200) {
      ElMessage.success('注册成功')
      await router.push('/login')
    }
  } finally {
    loadingValue.value = false
  }
}
</script>
