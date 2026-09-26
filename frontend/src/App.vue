<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { api, type ChatResponse, type DailyPoint, type DataQuality, type StoreOption, type Summary, type TopProduct } from './api'
echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const serviceStatus = ref('检查中…')
const start = ref('')
const end = ref('')
const storeId = ref('')
const stores = ref<StoreOption[]>([])
const summary = ref<Summary | null>(null)
const products = ref<TopProduct[]>([])
const quality = ref<DataQuality | null>(null)
const errorText = ref('')
const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let boardRequest = 0
const loading = ref(false)
const resizeChart = () => chart?.resize()

const sessionId = ref('')
const question = ref('')
const sending = ref(false)
const messages = ref<{ role: 'user' | 'assistant'; text: string; detail?: ChatResponse }[]>([])
const traceText = ref('还没有 trace。')
const traceSteps = ref<{ step: string; took_ms?: number; detail: unknown }[]>([])
const traceCalls = ref<unknown[]>([])
const traceErrors = ref<unknown[]>([])
const traceLoading = ref(false)
let traceRequest = 0

// 保留服务端完整调试记录，UI 不显示模型的私有思考过程。
function visibleTrace(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(visibleTrace)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !['reasoning_content', 'raw_reasoning', 'raw_response', 'prompt'].includes(key)).map(([key, item]) => [key, visibleTrace(item)]))
  }
  return value
}

async function showTrace(id: string) {
  const request = ++traceRequest
  traceLoading.value = true
  try {
    const trace = await api.trace(id)
    if (request !== traceRequest) return
    const safe = visibleTrace(trace) as typeof trace
    traceSteps.value = safe.steps
    traceCalls.value = safe.llm_calls
    traceErrors.value = safe.errors
    traceText.value = JSON.stringify(safe, null, 2)
  } catch (error) {
    if (request === traceRequest) traceText.value = error instanceof Error ? error.message : '追踪加载失败'
  } finally {
    if (request === traceRequest) traceLoading.value = false
  }
}

function newConversation() {
  if (sending.value) return
  sessionId.value = crypto.randomUUID()
  messages.value = []
  traceSteps.value = []
  traceCalls.value = []
  traceErrors.value = []
  traceText.value = '还没有 trace。'
  ++traceRequest
}

const removedLabels: Record<string, string> = {
  '1_unparseable_date': '日期无法解析',
  '2_empty_amount': '金额为空',
  '3_qty_le_zero': '数量无效',
  '4_store_not_in_stores': '门店不存在',
  '5_product_not_in_products': '商品不存在',
  '6_duplicate_row': '完全重复',
  note_unparseable_amount: '金额无法解析',
}

function filter() {
  return { start: start.value, end: end.value, store_id: storeId.value || undefined }
}

function draw(days: DailyPoint[]) {
  if (!chartEl.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  chart.setOption({
    grid: { left: 48, right: 16, top: 24, bottom: 28 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: days.map((day) => day.date.slice(5)) },
    yAxis: { type: 'value' },
    series: [{ type: 'line', smooth: true, showSymbol: false, data: days.map((day) => day.net_revenue), areaStyle: { opacity: 0.08 } }],
  })
}

async function loadBoard() {
  if (!start.value || !end.value) return
  const request = ++boardRequest
  if (start.value > end.value) {
    errorText.value = '起始日期不能晚于结束日期'
    return
  }
  loading.value = true
  errorText.value = ''
  try {
    const [sum, daily, top] = await Promise.all([
      api.summary(filter()),
      api.daily(filter()),
      api.topProducts(filter()),
    ])
    if (request !== boardRequest) return
    summary.value = sum
    products.value = top.products
    await nextTick()
    draw(daily.days)
  } catch (error) {
    if (request === boardRequest) errorText.value = error instanceof Error ? error.message : '指标加载失败'
  } finally {
    if (request === boardRequest) loading.value = false
  }
}

async function ask() {
  const text = question.value.trim()
  if (!text || sending.value) return
  question.value = ''
  messages.value.push({ role: 'user', text })
  sending.value = true
  try {
    const reply = await api.chat(sessionId.value, text)
    messages.value.push({ role: 'assistant', text: reply.answer, detail: reply })
    void showTrace(reply.trace_id)
  } catch (error) {
    messages.value.push({ role: 'assistant', text: error instanceof Error ? error.message : '问答失败' })
  } finally {
    sending.value = false
  }
}

onMounted(async () => {
  sessionId.value = crypto.randomUUID()
  try {
    const [health, storeList, qualityReport] = await Promise.all([
      api.health(),
      api.stores(),
      api.dataQuality(),
    ])
    serviceStatus.value = health.status === 'ok' ? `服务已连接 · ${health.llm_mode}` : '服务状态异常'
    stores.value = storeList.stores
    quality.value = qualityReport
    start.value = health.data_period.start
    end.value = health.data_period.end
  } catch {
    serviceStatus.value = '后端尚未启动'
  }
  window.addEventListener('resize', resizeChart)
})

onUnmounted(() => {
  ++boardRequest
  ++traceRequest
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
})

watch([start, end, storeId], () => {
  if (start.value && end.value) void loadBoard()
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

    <section class="filter-bar" aria-label="筛选条件">
      <label>日期起<input v-model="start" type="date" /></label>
      <label>日期止<input v-model="end" type="date" /></label>
      <label>门店
        <select v-model="storeId">
          <option value="">全部门店</option>
          <option v-for="store in stores" :key="store.store_id" :value="store.store_id">
            {{ store.store_id }} {{ store.store_name }}
          </option>
        </select>
      </label>
    </section>
    <p v-if="summary" class="summary-line">
      净营业额 {{ summary.net_revenue }} 元 · 订单 {{ summary.orders }} · 客单价 {{ summary.aov ?? '—' }} · 销量 {{ summary.qty }} · 退款 {{ summary.refund_amount }} 元
    </p>
    <p v-if="errorText" class="error">{{ errorText }}</p>
    <p v-if="loading" role="status" class="muted">正在更新看板…</p>

    <div class="grid">
      <section class="card wide">
        <div class="card-heading"><h2>营业额趋势</h2><span>净营业额</span></div>
        <div ref="chartEl" class="chart"></div>
      </section>
      <section class="card">
        <div class="card-heading"><h2>Top 10 商品</h2><span>按净营业额</span></div>
        <div class="table-scroll"><table>
          <thead><tr><th>商品</th><th>营业额（元）</th><th>销量</th></tr></thead>
          <tbody><tr v-for="item in products" :key="item.product_id"><td>{{ item.product_name }}</td><td>{{ item.net_revenue.toFixed(2) }}</td><td>{{ item.qty }}</td></tr></tbody>
        </table></div>
        <p v-if="!products.length" class="muted">这个范围内没有销售记录。</p>
      </section>
      <section class="card">
        <div class="card-heading"><h2>数据质量</h2><span>清洗</span></div>
        <dl v-if="quality" class="quality">
          <div><dt>原始行</dt><dd>{{ quality.cleaning_report.raw_rows }}</dd></div>
          <div><dt>保留行</dt><dd>{{ quality.cleaning_report.kept_rows }}</dd></div>
          <div v-for="(count, key) in quality.cleaning_report.removed" :key="key">
            <dt>{{ removedLabels[key] || key }}</dt><dd>{{ count }}</dd>
          </div>
        </dl>
      </section>
      <section class="card wide">
        <div class="card-heading"><h2>经营助手</h2><button :disabled="sending" @click="newConversation">新对话</button><span>{{ sessionId.slice(0, 8) }}</span></div>
        <div class="chat">
          <p v-if="!messages.length" class="muted">问题会带上当前会话，追问不会串到别的会话。</p>
          <article v-for="(message, index) in messages" :key="index" :class="message.role">
            <p>{{ message.text }}</p>
            <ul v-if="message.detail?.citations.length">
              <li v-for="(citation, citeIndex) in message.detail.citations" :key="citeIndex">
                {{ citation.doc_id }}：{{ citation.quote }}
              </li>
            </ul>
            <p v-if="message.detail?.data_evidence.length" class="muted">
              数据证据 {{ message.detail.data_evidence.length }} 条 · {{ message.detail.answer_type }} · {{ message.detail.trace_id }}
            </p>
            <details v-for="(evidence, evidenceIndex) in message.detail?.data_evidence" :key="evidenceIndex">
              <summary>查看查询证据 · {{ evidence.tool || 'SQL' }}</summary>
              <pre class="trace">{{ JSON.stringify(evidence, null, 2) }}</pre>
            </details>
            <button v-if="message.detail" @click="showTrace(message.detail.trace_id)">查看本次追踪</button>
          </article>
        </div>
        <form class="ask" @submit.prevent="ask">
          <input v-model="question" aria-label="向经营助手提问" maxlength="4000" placeholder="例如：7 月净营业额是多少？" />
          <button type="submit" :disabled="sending">{{ sending ? '思考中…' : '发送' }}</button>
        </form>
      </section>
      <section class="card">
        <div class="card-heading"><h2>调试追踪</h2><span>trace</span></div>
        <p v-if="traceLoading" role="status">正在加载追踪…</p>
        <details v-for="(step, index) in traceSteps" :key="index">
          <summary>{{ step.step }} <span v-if="step.took_ms != null">· {{ step.took_ms }} ms</span></summary>
          <pre class="trace">{{ JSON.stringify(step.detail, null, 2) }}</pre>
        </details>
        <details v-for="(call, index) in traceCalls" :key="`llm-${index}`"><summary>模型请求与输出 · {{ index + 1 }}</summary><pre class="trace">{{ JSON.stringify(call, null, 2) }}</pre></details>
        <pre v-if="traceErrors.length" class="trace error">{{ JSON.stringify(traceErrors, null, 2) }}</pre>
        <details><summary>完整追踪 JSON（隐藏思考内容）</summary><pre class="trace">{{ traceText }}</pre></details>
      </section>
    </div>
  </div>
</template>
