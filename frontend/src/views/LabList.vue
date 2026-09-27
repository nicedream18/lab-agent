<template>
  <div class="workspace-page lab-catalog-page">
    <div class="page-heading">
      <div>
        <div class="page-kicker">RESERVABLE SPACES</div>
        <h2 class="page-title">实验室列表</h2>
        <p class="page-description">浏览可预约空间与开放时段，选择实验室或具体设备开始预约。</p>
      </div>
    </div>

    <div class="catalog-toolbar">
      <div class="filter-controls">
        <el-input
          placeholder="请输入名称查询"
          v-model="params.keywords"
          clearable
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        ></el-input>
        <el-button type="primary" :icon="Search" @click="handleSearch">查询</el-button>
      </div>
      <span class="result-count">{{ total }} 个空间</span>
    </div>

    <el-row :gutter="16" class="lab-grid" v-loading="loading">
      <el-col v-for="item in tableData" :key="item.id" :xs="24" :sm="12" :lg="8" :xl="6">
        <el-card shadow="hover" class="lab-card">
          <div class="lab-cover">
            <img v-if="item.img" :src="item.img" :alt="item.name" />
            <div v-else class="lab-placeholder" aria-hidden="true">
              <el-icon><OfficeBuilding /></el-icon>
              <span>LAB SPACE</span>
            </div>
            <el-tag class="lab-status" type="success" effect="dark">开放预约</el-tag>
          </div>
          <div class="lab-card-content">
            <h3 class="lab-card-title">{{ item.name }}</h3>
            <div class="lab-fact">
              <span>位置</span><strong>{{ item.location || '待完善' }}</strong>
            </div>
            <div class="lab-fact">
              <span>容纳人数</span><strong>{{ item.capacity }} 人</strong>
            </div>
            <div class="lab-fact">
              <span>开放时段</span>
              <strong>{{
                item.open_time && item.close_time
                  ? `${item.open_time} – ${item.close_time}`
                  : '待定'
              }}</strong>
            </div>
            <div class="lab-card-actions">
              <el-button @click="handleViewEquipment(item)">查看设备</el-button>
              <el-button type="primary" @click="handleReserve(item)">预约空间</el-button>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-empty v-if="!loading && !tableData.length" description="没有找到符合条件的实验室" />

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

    <reserve-dialog
      v-model:visible="reserveVisible"
      :lab-id="currentLab?.id"
      :lab-name="currentLab?.name"
    ></reserve-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getLabPageList } from '@/api/lab'
import { OfficeBuilding, Search } from '@element-plus/icons-vue'
import router from '@/router'
import ReserveDialog from '@/components/ReserveDialog.vue'

const params = reactive({
  page: 1,
  page_size: 10,
  keywords: ''
})
const loading = ref(false)
const tableData = ref([])
const total = ref(0)

const reserveVisible = ref(false)
const currentLab = ref(null)

const handleViewEquipment = (lab) => {
  router.push({ path: '/manager/lab-equipment', query: { lab_id: lab.id } })
}

const handleReserve = (lab) => {
  currentLab.value = lab
  reserveVisible.value = true
}

// 加载分页数据
const load = async () => {
  loading.value = true
  try {
    const res = await getLabPageList(params)
    if (res.code === 200) {
      tableData.value = res.data?.list
      total.value = res.data?.total
    }
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  params.page = 1
  load()
}

onMounted(() => {
  load()
})
</script>

<style scoped>
.catalog-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.result-count {
  flex: 0 0 auto;
  color: var(--app-muted);
  font-size: 12px;
}

.lab-grid {
  row-gap: 16px;
}

.lab-card {
  height: 100%;
  overflow: hidden;
}

.lab-card :deep(.el-card__body) {
  padding: 0;
}

.lab-cover {
  position: relative;
  height: 168px;
  overflow: hidden;
  background: #e9f1eb;
}

.lab-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.lab-placeholder {
  display: flex;
  height: 100%;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 8px;
  color: #72917f;
}

.lab-placeholder .el-icon {
  font-size: 32px;
}

.lab-placeholder span {
  font-size: 10px;
  font-weight: 800;
}

.lab-status {
  position: absolute;
  top: 12px;
  right: 12px;
}

.lab-card-content {
  padding: 17px;
}

.lab-card-title {
  margin-bottom: 14px;
  color: var(--app-ink);
  font-size: 17px;
  font-weight: 750;
}

.lab-fact {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 7px 0;
  border-bottom: 1px solid #edf2ee;
  font-size: 12px;
}

.lab-fact span {
  flex: 0 0 auto;
  color: var(--app-muted);
}

.lab-fact strong {
  overflow: hidden;
  color: #405248;
  font-weight: 600;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lab-card-actions {
  display: flex;
  gap: 8px;
  margin-top: 15px;
}

.lab-card-actions .el-button {
  flex: 1;
  margin-left: 0;
}

@media (max-width: 640px) {
  .catalog-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .catalog-toolbar .filter-controls {
    width: 100%;
  }
}
</style>
