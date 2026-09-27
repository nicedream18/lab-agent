import { ref } from 'vue'
import { getUserInfo, setToken, setUserInfo } from './auth'
const userInfo = ref(getUserInfo())

export function useUser(data) {
  function saveLoginData(data) {
    setToken(data.token)
    setUserInfo(data.user)
    userInfo.value = data.user
  }

  function updateUser(data) {
    setUserInfo(data)
    userInfo.value = data
  }

  function reloadUser() {
    userInfo.value = getUserInfo()
  }

  return {
    userInfo,
    saveLoginData,
    updateUser,
    reloadUser
  }
}
