<template>
  <div class="workspace-page">
    <div class="page-heading">
      <div>
        <div class="page-kicker">SPACE INVENTORY</div>
        <h2 class="page-title">{{ lab?.name || '实验室设备' }}</h2>
        <p class="page-description">查看设备规格与可用状态，选择空间或设备提交预约。</p>
      </div>
      <div class="page-actions">
        <el-button @click="router.push('/manager/lablist')">返回实验室列表</el-button>
        <el-button type="primary" @click="handlReserveLab">预约实验室</el-button>
      </div>
    </div>

    <section v-if="lab" class="lab-overview">
      <p v-if="lab.description" class="lab-overview-description">{{ lab.description }}</p>
      <div class="lab-overview-facts">
        <span><small>位置</small>{{ lab.location || '待完善' }}</span>
        <span><small>容量</small>{{ lab.capacity }} 人</span>
        <span>
          <small>开放时间</small>
          {{ lab.open_time && lab.close_time ? `${lab.open_time} – ${lab.close_time}` : '待定' }}
        </span>
      </div>
    </section>

    <el-card shadow="never" class="data-card">
      <el-table :data="tableData" style="width: 100%" v-loading="loading">
        <el-table-column prop="img" label="图片" width="100">
          <template #default="{ row }">
            <div style="min-height: 50px; display: flex; align-items: center">
              <el-image
                v-if="row.img"
                style="display: block; width: 50px; height: 50px; border-radius: 6px"
                :src="row.img"
                :preview-src-list="[row.img]"
                :preview-teleported="true"
                alt=""
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="设备名称" />

        <el-table-column prop="spec" label="型号规格" />
        <el-table-column prop="quantity" label="数量" width="80" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 1 ? 'success' : 'danger'">
              {{ row.status === 1 ? '正常' : '维修' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button
              :disabled="!row.status"
              type="primary"
              text
              bg
              @click="handleReserveEquipment(row)"
              >预约</el-button
            >
          </template>
        </el-table-column>
      </el-table>

      <div class="table-pagination">
        <el-pagination
          v-model:current-page="params.page"
          v-model:page-size="params.page_size"
          :total="total"
          background
          layout="total, prev, pager, next"
          @current-change="loadEquipment"
        />
      </div>
    </el-card>

    <reserve-dialog
      v-model:visible="reserveVisible"
      :lab-id="lab?.id"
      :lab-name="lab?.name"
      :equipment-id="currentEquipment?.id"
      :equipment-name="currentEquipment?.name"
    ></reserve-dialog>
  </div>
</template>

<script setup>
import { getLab } from '@/api/lab'
import { ElMessage } from 'element-plus'
import { ref, reactive, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getEquipmentPageList } from '@/api/equipment'
import ReserveDialog from '@/components/ReserveDialog.vue'

const route = useRoute()
const router = useRouter()

const lab = ref(null)
const params = reactive({
  page: 1,
  page_size: 10,
  lab_id: Number(route.query.lab_id) // "8" -> 8
})
const loading = ref(false)
const tableData = ref([])
const total = ref(0)

const reserveVisible = ref(false)
const currentEquipment = ref(null)

// 预约整个实验室：不传设备
const handlReserveLab = () => {
  currentEquipment.value = null
  reserveVisible.value = true
}

// 预约某台设备
const handleReserveEquipment = (row) => {
  currentEquipment.value = row
  reserveVisible.value = true
}

// 校验路由参数 lab_id：不合法时提示并阻止后续请求，
// 避免发出 lab_id=NaN 这种请求让后端返回 422
const ensureLabId = () => {
  const id = Number(route.query.lab_id)
  if (!id) {
    ElMessage.error('缺少实验室查询条件 [lab_id]')
    return false
  }
  params.lab_id = id
  return true
}

const loadLab = async () => {
  const res = await getLab(params.lab_id)
  if (res.code === 200) {
    lab.value = res.data
  }
}

// 加载设备分页数据
const loadEquipment = async () => {
  loading.value = true
  try {
    const res = await getEquipmentPageList(params)
    if (res.code === 200) {
      tableData.value = res.data?.list ?? []
      total.value = res.data?.total ?? 0
    }
  } catch (e) {
    // request.js 里已经弹过错误提示，这里兜住避免未处理的 Promise 报错
  } finally {
    loading.value = false
  }
}

const load = () => {
  if (!ensureLabId()) return
  params.page = 1
  lab.value = null
  tableData.value = []
  total.value = 0
  loadLab()
  loadEquipment()
}

onMounted(load)

// 同一个组件实例在“实验室A → 实验室B”之间跳转时不会重新挂载，
// 需要监听 query 变化手动重新加载（否则页面会残留上一个实验室的数据）
watch(() => route.query.lab_id, load)
</script>

<style scoped>
.lab-overview {
  margin-bottom: 18px;
  padding: 17px 20px;
  border: 1px solid #dce8df;
  border-left: 3px solid var(--app-green);
  border-radius: 10px;
  background: #f8fbf8;
}

.lab-overview-description {
  margin-bottom: 13px;
  color: #53645b;
  line-height: 1.6;
}

.lab-overview-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 32px;
  color: var(--app-ink);
  font-size: 13px;
}

.lab-overview-facts span {
  display: flex;
  gap: 8px;
}

.lab-overview-facts small {
  color: var(--app-muted);
}

@media (max-width: 640px) {
  .lab-overview-facts {
    flex-direction: column;
    gap: 8px;
  }
}
</style>
