<template>
  <div class="workspace-page">
    <div class="page-heading">
      <div>
        <div class="page-kicker">MY BOOKINGS</div>
        <h2 class="page-title">我的预约</h2>
        <p class="page-description">跟踪审核进度、查看预约安排，待审核记录可随时取消。</p>
      </div>
    </div>

    <el-card shadow="never" class="data-card">
      <div class="filter-bar">
        <div class="filter-controls">
          <el-select
            placeholder="请选择状态"
            v-model="params.status"
            clearable
            @change="handleSearch"
            @clear="handleSearch"
          >
            <el-option :value="0" label="待审核" />
            <el-option :value="1" label="已通过" />
            <el-option :value="2" label="已拒绝" />
            <el-option :value="3" label="已取消" />
          </el-select>
          <el-button type="primary" @click="handleSearch">查询</el-button>
        </div>
        <span class="result-count">共 {{ total }} 条预约</span>
      </div>
      <el-table :data="tableData" style="width: 100%" v-loading="loading">
        <el-table-column prop="lab_name" label="实验室" />
        <el-table-column label="预约类型">
          <template #default="{ row }">
            <el-tag :type="row.type === '实验室' ? 'success' : 'primary'">{{ row.type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="设备">
          <template #default="{ row }">
            {{ row.equipment_name || '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="date" label="日期" />
        <el-table-column label="预约时段">
          <template #default="{ row }"> {{ row.start_time }} - {{ row.end_time }} </template>
        </el-table-column>
        <el-table-column prop="remark" label="备注" />
        <el-table-column label="预约状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button v-if="row.status === 0" type="danger" text bg @click="handleCancel(row)"
              >取消</el-button
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
          @current-change="load"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { cancelReservationApi, getReservationPageList } from '@/api/reservation'
import { ref, reactive, onMounted } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
const statusType = (status) => {
  return { 0: 'warning', 1: 'success', 2: 'danger', 3: 'info' }[status] || '-'
}
const statusText = (status) => {
  return { 0: '待审核', 1: '已通过', 2: '已拒绝', 3: '已取消' }[status] || '-'
}
const loading = ref(false)
const tableData = ref([])
const params = reactive({
  page: 1,
  page_size: 10,
  status: null
})
const total = ref(0)

const load = async () => {
  loading.value = true
  try {
    const res = await getReservationPageList(params)
    if (res.code === 200) {
      tableData.value = res.data?.list ?? []
      total.value = res.data?.total ?? 0
    }
  } catch (e) {
    // request.js 已统一提示，这里兜住避免 unhandled rejection
  } finally {
    loading.value = false
  }
}

const handleCancel = (row) => {
  ElMessageBox.confirm(`确认取消 [${row.lab_name}] 的预约？`, '确认取消', { type: 'warning' })
    .then(async () => {
      try {
        const res = await cancelReservationApi(row.id)
        if (res.code === 200) {
          ElMessage.success('取消预约成功')
          load()
        }
      } catch (e) {
        // request.js 已统一提示
      }
    })
    .catch(() => {
      // 用户点了取消，不做处理
    })
}

const handleSearch = () => {
  params.page = 1
  load()
}

onMounted(() => {
  load()
})
</script>
