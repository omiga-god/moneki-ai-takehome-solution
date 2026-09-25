<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from './api'

const serviceStatus = ref('检查中…')

onMounted(async () => {
  try {
    const health = await api.health()
    serviceStatus.value = health.status === 'ok' ? `服务已连接 · ${health.llm_mode}` : '服务状态异常'
  } catch {
    serviceStatus.value = '后端尚未启动'
  }
})
</script>

<template>
  <div class="shell">
    <header class="header">
      <div>
        <p class="eyebrow">MONEKI · OPERATIONS</p>
        <h1>经营工作台</h1>
        <p class="subtitle">数据看板与证据可追溯的经营问答</p>
      </div>
      <span class="status">{{ serviceStatus }}</span>
    </header>

    <main>
      <section class="filter-bar" aria-label="筛选条件">
        <div><span class="label">日期范围</span><strong>待连接指标 API</strong></div>
        <div><span class="label">门店</span><strong>待从数据库加载</strong></div>
      </section>

      <div class="grid">
        <section class="card wide">
          <div class="card-heading"><h2>营业额趋势</h2><span>第一关</span></div>
          <p class="placeholder">接入 /api/metrics/daily 后绘制图表；此处不使用示例销售数字。</p>
        </section>
        <section class="card">
          <div class="card-heading"><h2>Top 10 商品</h2><span>第一关</span></div>
          <p class="placeholder">从后端真实商品排行接口加载。</p>
        </section>
        <section class="card">
          <div class="card-heading"><h2>数据质量</h2><span>第一关</span></div>
          <p class="placeholder">展示清洗前后行数和各类剔除原因。</p>
        </section>
        <section class="card wide">
          <div class="card-heading"><h2>经营助手</h2><span>第三关</span></div>
          <p class="placeholder">实现聊天、引用、数据证据和追问会话。</p>
        </section>
        <section class="card">
          <div class="card-heading"><h2>调试追踪</h2><span>第四关</span></div>
          <p class="placeholder">按 trace_id 展示检索、工具、提示词和耗时。</p>
        </section>
      </div>
    </main>
  </div>
</template>
