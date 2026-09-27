import axios from 'axios'
import { ElMessage } from 'element-plus'
import { getToken, logout } from './auth'
import router from '@/router'
const service = axios.create({
  // 未配置时使用相对路径，由 Vite 的 server.proxy 转发到后端
  baseURL: import.meta.env.VITE_API_BASE_URL || '',
  timeout: 5000
})

// 请求拦截器的配置
service.interceptors.request.use(
  (config) => {
    const token = getToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (err) => Promise.reject(err)
)

// 返回的拦截器配置
service.interceptors.response.use(
  (res) => {
    const data = res.data
    if (data.code !== 200) {
      ElMessage.error(data.message || '请求失败')
      if (data.code === 401) {
        logout()
        router.push('/login')
        return Promise.reject(data)
      }
    }
    return data
  },
  (err) => {
    if (err.response) {
      const httpStatusCode = err.response.status
      if (httpStatusCode === 401) {
        ElMessage.error('登录状态已失效，请重新登录')
        logout()
        router.push('/login')
      } else {
        ElMessage.error(err.response.data?.message || '请求失败')
      }
    } else {
      // 后端没启动，网络异常了
      ElMessage.error('网络异常，请检查后端服务')
    }
    return Promise.reject(err)
  }
)

export default service
