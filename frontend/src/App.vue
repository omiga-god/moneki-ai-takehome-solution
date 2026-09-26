<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { api, type ChatResponse, type DailyPoint, type DataQuality, type StoreOption, type Summary, type TopProduct } from './api'
echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const number = (value: number | null | undefined, decimals = 0) => value == null ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
const suggestions = ['7 月净营业额是多少？', '退款政策是什么？', '618 当天 S02 牛肉 poke 达到目标了吗？']
const activeSection = ref(window.location.hash || '#overview')
const syncSection = () => { activeSection.value = window.location.hash || '#overview' }
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
  traceLoading.value = false
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
    color: ['#3159d9'],
    animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    grid: { left: 58, right: 20, top: 24, bottom: 32 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', boundaryGap: false, axisLine: { lineStyle: { color: '#dbe2ec' } }, axisTick: { show: false }, axisLabel: { color: '#65738b' }, data: days.map((day) => day.date.slice(5)) },
    yAxis: { type: 'value', axisLabel: { color: '#65738b' }, splitLine: { lineStyle: { color: '#edf0f5', type: 'dashed' } } },
    series: [{ type: 'line', smooth: false, showSymbol: false, data: days.map((day) => day.net_revenue), areaStyle: { opacity: 0.08 } }],
  })
}

async function loadBoard() {
  if (!start.value || !end.value) return
  const request = ++boardRequest
  if (start.value > end.value) {
    loading.value = false
    summary.value = null
    products.value = []
    chart?.clear()
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
    if (request === boardRequest) {
      errorText.value = error instanceof Error ? error.message : '指标加载失败'
      summary.value = null
      products.value = []
      chart?.clear()
    }
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
  window.addEventListener('hashchange', syncSection)
  window.addEventListener('resize', resizeChart)
})

onUnmounted(() => {
  ++boardRequest
  ++traceRequest
  window.removeEventListener('hashchange', syncSection)
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
})

watch([start, end, storeId], () => {
  if (start.value && end.value) void loadBoard()
})
</script>

<template>
  <a class="skip-link" href="#overview">跳到经营概览</a>
  <aside class="sidebar">
    <a href="#overview" class="brand"><span class="brand-mark" aria-hidden="true">m.</span><span>moneki<span class="brand-ai">.ai</span><small>经营数据工作台</small></span></a>
    <p class="nav-label">工作空间</p>
    <nav aria-label="主导航">
      <a href="#overview" :class="{ active: activeSection === '#overview' }" :aria-current="activeSection === '#overview' ? 'location' : undefined"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z" /></svg>经营概览</a>
      <a href="#assistant" :class="{ active: activeSection === '#assistant' }" :aria-current="activeSection === '#assistant' ? 'location' : undefined"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11a8 8 0 0 1-8 8H7l-4 3V11a9 9 0 0 1 18 0Z M8 10h8 M8 14h5" /></svg>经营助手</a>
      <a href="#quality" :class="{ active: activeSection === '#quality' }" :aria-current="activeSection === '#quality' ? 'location' : undefined"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5h12 M9 12h12 M9 19h12 M3 5l1 1 2-2 M3 12l1 1 2-2 M3 19l1 1 2-2" /></svg>数据质量</a>
      <a href="#trace" :class="{ active: activeSection === '#trace' }" :aria-current="activeSection === '#trace' ? 'location' : undefined"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5z M9 8h6 M9 12h6 M9 16h3" /></svg>查询追踪</a>
    </nav>
    <div class="sidebar-note"><span class="live-dot"></span>每一个结论，都有依据<p>连接销售数据与内部知识，让经营决策更清晰。</p></div>
  </aside>
  <main class="shell" id="overview">
    <header class="header">
      <div>
        <p class="eyebrow">WORKSPACE / OVERVIEW</p>
        <h1>看清经营的每一天<span class="heading-dot">.</span></h1>
        <p class="subtitle">从门店表现到经营洞察，在这里找到答案。</p>
      </div>
      <span class="status" role="status"><span class="live-dot"></span>{{ serviceStatus }}</span>
    </header>

    <section class="filter-bar" aria-label="筛选条件">
      <div class="filter-title">经营概览<small>按时间与门店查看</small></div><label>开始日期<input v-model="start" type="date" /></label>
      <label>结束日期<input v-model="end" type="date" /></label>
      <label>门店
        <select v-model="storeId">
          <option value="">全部门店</option>
          <option v-for="store in stores" :key="store.store_id" :value="store.store_id">
            {{ store.store_id }} {{ store.store_name }}
          </option>
        </select>
      </label>
      <button class="refresh-button" :disabled="loading" @click="loadBoard">{{ loading ? '更新中…' : '刷新数据' }}</button>
    </section>
    <section class="metrics" aria-label="核心经营指标" :aria-busy="loading">
      <article class="metric primary"><span>净营业额 <small>CNY</small></span><strong>{{ loading ? '…' : number(summary?.net_revenue, 2) }}</strong><p>销售收入扣除退款后的金额</p></article>
      <article class="metric"><span>订单数</span><strong>{{ loading ? '…' : number(summary?.orders) }}<small>笔</small></strong><p>所选范围内的有效订单</p></article>
      <article class="metric"><span>客单价</span><strong>{{ loading ? '…' : number(summary?.aov, 2) }}<small>元</small></strong><p>每笔订单的平均消费</p></article>
      <article class="metric"><span>商品销量</span><strong>{{ loading ? '…' : number(summary?.qty) }}<small>份</small></strong><p>退款金额 {{ loading ? '…' : number(summary?.refund_amount, 2) }} 元</p></article>
    </section>
    <p v-if="errorText" class="error" role="alert">{{ errorText }}</p>
    <p v-if="loading" role="status" class="muted">正在更新看板…</p>

    <div class="grid">
      <section class="card wide trend-card">
        <div class="card-heading"><h2>营业额趋势</h2><span>净营业额</span></div>
        <p class="section-note">所选日期内的每日净营业额 · 单位：元</p><div ref="chartEl" class="chart" role="img" aria-label="每日净营业额折线图"></div>
      </section>
      <section class="card products-card">
        <div class="card-heading"><h2>热销商品</h2><span>按净营业额</span></div>
        <div class="table-scroll"><table>
          <thead><tr><th>排名 / 商品</th><th>营业额（元）</th><th>销量</th></tr></thead>
          <tbody><tr v-for="(item, rank) in products" :key="item.product_id"><td><span class="rank-number" :class="{ podium: rank < 3 }">{{ String(rank + 1).padStart(2, '0') }}</span>{{ item.product_name }}</td><td>{{ number(item.net_revenue, 2) }}</td><td>{{ item.qty }}</td></tr></tbody>
        </table></div>
        <p v-if="!products.length" class="muted">这个范围内没有销售记录。</p>
      </section>
      <section class="card quality-card" id="quality">
        <div class="card-heading"><h2>数据质量</h2><span>清洗</span></div>
        <div v-if="quality" class="quality-score"><strong>{{ quality.cleaning_report.raw_rows ? (quality.cleaning_report.kept_rows / quality.cleaning_report.raw_rows * 100).toFixed(1) : '0.0' }}<small>%</small></strong><span>有效数据保留率</span><meter min="0" :max="quality.cleaning_report.raw_rows || 1" :value="quality.cleaning_report.kept_rows" aria-label="有效数据保留率"></meter></div>
        <dl v-if="quality" class="quality">
          <div><dt>原始行</dt><dd>{{ quality.cleaning_report.raw_rows }}</dd></div>
          <div><dt>保留行</dt><dd>{{ quality.cleaning_report.kept_rows }}</dd></div>
          <div v-for="(count, key) in quality.cleaning_report.removed" :key="key">
            <dt>{{ removedLabels[key] || key }}</dt><dd>{{ count }}</dd>
          </div>
        </dl>
      </section>
      <section class="card wide assistant-card" id="assistant">
        <div class="card-heading"><h2>经营助手</h2><button :disabled="sending" @click="newConversation">新对话</button></div>
        <div class="chat" role="log" aria-label="经营问答记录" aria-live="polite">
          <div v-if="!messages.length" class="chat-empty"><div class="assistant-symbol" aria-hidden="true">m.</div><h3>让数据回答你的问题</h3><p>查询销售表现、查阅内部制度，或将两者结合分析。<br>回答附带数据证据与文档出处。</p><div class="suggestions"><button v-for="sample in suggestions" :key="sample" @click="question = sample">{{ sample }} <span aria-hidden="true">↗</span></button></div></div>
          <article v-for="(message, index) in messages" :key="index" :class="message.role">
            <span class="message-label">{{ message.role === 'user' ? '你' : 'MONEKI 助手' }}</span><p>{{ message.text }}</p>
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
        <p v-if="sending" role="status" class="muted">正在查询数据与相关文档…</p><form class="ask" @submit.prevent="ask">
          <input v-model="question" aria-label="向经营助手提问" maxlength="4000" placeholder="例如：7 月净营业额是多少？" />
          <button type="submit" :disabled="sending || !question.trim()">{{ sending ? '思考中…' : '发送' }}</button>
        </form>
      </section>
      <section class="card trace-card" id="trace">
        <div class="card-heading"><h2>查询追踪</h2><span>trace</span></div>
        <p v-if="!traceSteps.length && !traceLoading" class="section-note">提问后，在这里查看检索、数据查询和回答生成的过程。</p><p v-if="traceLoading" role="status">正在加载追踪…</p>
        <details v-for="(step, index) in traceSteps" :key="index">
          <summary>{{ step.step }} <span v-if="step.took_ms != null">· {{ step.took_ms }} ms</span></summary>
          <pre class="trace">{{ JSON.stringify(step.detail, null, 2) }}</pre>
        </details>
        <details v-for="(call, index) in traceCalls" :key="`llm-${index}`"><summary>模型请求与输出 · {{ index + 1 }}</summary><pre class="trace">{{ JSON.stringify(call, null, 2) }}</pre></details>
        <pre v-if="traceErrors.length" class="trace error">{{ JSON.stringify(traceErrors, null, 2) }}</pre>
        <details><summary>完整追踪 JSON（隐藏思考内容）</summary><pre class="trace">{{ traceText }}</pre></details>
      </section>
    </div>
    <footer>MONEKI WORKSPACE <span>经营洞察 · 有据可循</span></footer>
  </main>
</template>
