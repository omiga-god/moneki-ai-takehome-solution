// Contract shapes only. Business behavior belongs in the implementation phase.
export type AnswerType = 'data' | 'doc' | 'hybrid' | 'refusal' | 'clarify'

export interface MetricsFilter {
  start: string
  end: string
  store_id?: string
  product_id?: string
}

export interface Summary {
  start: string
  end: string
  store_id: string | null
  product_id: string | null
  net_revenue: number
  refund_amount: number
  orders: number
  aov: number | null
  qty: number
}

export interface DailyPoint {
  date: string
  net_revenue: number
  orders: number
  aov: number | null
}

export interface RetrievalResult {
  doc_id: string
  chunk_id: string
  score: number
  text: string
}

export interface Citation {
  doc_id: string
  quote: string
}

export interface DataEvidence {
  tool?: string
  params?: Record<string, unknown>
  sql?: string
  result: unknown
}

export interface ChatResponse {
  answer: string
  answer_type: AnswerType
  citations: Citation[]
  data_evidence: DataEvidence[]
  trace_id: string
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${path}`)
  return response.json() as Promise<T>
}

function query(filter: MetricsFilter): string {
  const params = new URLSearchParams({ start: filter.start, end: filter.end })
  if (filter.store_id) params.set('store_id', filter.store_id)
  if (filter.product_id) params.set('product_id', filter.product_id)
  return params.toString()
}

export const api = {
  health: () => getJson<{ status: string; llm_mode: 'live' | 'mock'; kb_docs: number; kb_chunks: number; valid_sales_rows: number }>('/api/health'),
  summary: (filter: MetricsFilter) => getJson<Summary>(`/api/metrics/summary?${query(filter)}`),
  daily: (filter: MetricsFilter) => getJson<{ days: DailyPoint[] }>(`/api/metrics/daily?${query(filter)}`),
  trace: (id: string) => getJson<Record<string, unknown>>(`/api/trace/${encodeURIComponent(id)}`),
  retrieve: async (queryText: string, topK = 5): Promise<{ results: RetrievalResult[] }> => {
    const response = await fetch('/api/retrieve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: queryText, top_k: topK }),
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}: /api/retrieve`)
    return response.json() as Promise<{ results: RetrievalResult[] }>
  },
  chat: async (sessionId: string, question: string): Promise<ChatResponse> => {
    const response = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, question }),
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}: /api/chat`)
    return response.json() as Promise<ChatResponse>
  },
}
