export type Overview = {
  stock_count: number;
  risk_count: number;
  latest_trade_date: string;
  latest_symbol_count: number;
  daily_count: number;
  analysis_count: number;
  three_day_analysis_count: number;
};

export type RealtimeAccount = {
  account_id: string;
  initial_cash: number;
  cash: number;
  market_value: number;
  total_asset: number;
  realized_pnl: number;
};

export type SimulationPosition = {
  symbol: string;
  name: string;
  quantity: number;
  available_quantity: number;
  avg_cost: number;
  last_price: number;
  market_value: number;
  floating_pnl: number;
};

export type RealtimeDecisionMode = 'rules' | 'llm_review';

export type RealtimeDecisionSummary = {
  decision_time: string | null;
  decision_count: number;
  llm_count: number;
  fallback_count: number;
  decision_mode: RealtimeDecisionMode | string | null;
};

export type RealtimeMarketStatus = {
  now: string;
  marketOpen: boolean;
  session: 'morning' | 'afternoon' | 'closed' | string;
  reason: string;
};

export type RealtimeSignal = {
  id: number;
  signal_time: string;
  symbol: string;
  name: string;
  latest_price: number;
  pct_change: number | null;
  trend_score: number;
  momentum_score: number;
  liquidity_score: number;
  risk_score: number;
  final_score: number;
  signal_action: 'BUY' | 'SELL' | 'WATCH' | 'HOLD';
  confidence: number;
  reason: string;
  risk: string;
  decision_source?: string | null;
  llm_action?: 'BUY' | 'SELL' | 'WATCH' | 'HOLD' | null;
  llm_score?: number | null;
  llm_reason?: string | null;
  llm_risk?: string | null;
  llm_fallback?: number | boolean | null;
};

export type SimulationOrder = {
  id: number;
  order_time: string;
  symbol: string;
  name: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  amount: number;
  fee: number;
  status: string;
  reason: string;
};

export type RealtimeDashboard = {
  account: RealtimeAccount;
  positions: SimulationPosition[];
  signals: RealtimeSignal[];
  orders: SimulationOrder[];
  latestSignalTime: string | null;
  quoteSummary: {
    quote_count: number;
    snapshot_time: string | null;
  };
  decisionSummary: RealtimeDecisionSummary;
  marketStatus: RealtimeMarketStatus;
};

export type RealtimeRunResult = {
  snapshotTime: string;
  quoteCount: number;
  signalCount: number;
  buyCount: number;
  sellCount: number;
  orderCount: number;
  decisionCount: number;
  decisionMode: RealtimeDecisionMode | string;
  llmUsed: boolean;
  llmFallback: boolean;
  marketOpen: boolean;
  skipped: boolean;
  skipReason: string;
  marketSession: string;
};

export type CallAuctionMarketStatus = {
  now: string;
  marketOpen: boolean;
  collectOpen: boolean;
  decisionOpen: boolean;
  session: 'pre_call_auction' | 'call_auction' | 'closed' | string;
  reason: string;
  window: string;
  collectWindow: string;
  decisionWindow: string;
};

export type CallAuctionRun = {
  id?: number;
  runId?: number | null;
  tradeDate: string | null;
  snapshotTime: string | null;
  triggerType: string;
  status: string;
  quoteCount: number;
  validQuoteCount: number;
  candidateCount: number;
  pickCount: number;
  llmRequired: boolean;
  llmSuccess: boolean;
  marketSession: string;
  skipped?: boolean;
  skipReason: string;
  errorMessage: string;
  summary?: Record<string, unknown>;
  createdAt?: string | null;
};

export type CallAuctionPick = {
  id: number;
  runId: number;
  rank: number;
  tradeDate: string;
  snapshotTime: string;
  symbol: string;
  name: string;
  latestPrice: number;
  pctChange: number;
  volume: number;
  amount: number;
  volumeRatio: number;
  turnoverRate: number;
  priceScore: number;
  volumeScore: number;
  trendScore: number;
  riskScore: number;
  quantScore: number;
  llmScore: number | null;
  finalScore: number;
  action: 'BUY_CANDIDATE' | 'WATCH' | string;
  reason: string;
  risk: string;
};

export type CallAuctionQuoteSummary = {
  quoteCount: number;
  validPriceCount: number;
  firstSampleTime: string | null;
  latestSampleTime: string | null;
  totalSampleCount: number;
};

export type CallAuctionDashboard = {
  latestRun: CallAuctionRun | null;
  picks: CallAuctionPick[];
  marketStatus: CallAuctionMarketStatus;
  quoteSummary: CallAuctionQuoteSummary;
  autoIntervalSeconds: number;
};

export type StockRow = {
  symbol: string;
  name: string;
  exchange: string;
  is_risk: number;
  trade_date?: string | null;
  close_price?: number | null;
  pct_change?: number | null;
  turnover_rate?: number | null;
  amount?: number | null;
};

export type SegmentItem = {
  name: string;
  value: number;
};

export type AmountTopItem = {
  symbol: string;
  name: string;
  amount: number | null;
  pct_change: number | null;
};

export type Segments = {
  tradeDate: string | null;
  exchangeDistribution: SegmentItem[];
  riskDistribution: SegmentItem[];
  pctDistribution: SegmentItem[];
  amountTop: AmountTopItem[];
};

export type HistoryPoint = {
  trade_date: string;
  open_price: number | null;
  close_price: number | null;
  high_price: number | null;
  low_price: number | null;
  volume: number | null;
  amount: number | null;
  pct_change: number | null;
  turnover_rate: number | null;
};

export type HistoryResponse = {
  stock: {
    symbol: string;
    name: string;
    exchange: string;
  };
  data: HistoryPoint[];
};

export type AnalysisPick = {
  rank_no: number;
  symbol: string;
  name: string;
  trade_date: string;
  quant_score: number;
  llm_score: number;
  final_score: number;
  expected_direction: string;
  reason: string;
  risk: string;
};

export type AnalysisResponse = {
  analysisDate: string | null;
  data: AnalysisPick[];
};

export type ThreeDayAnalysisPick = AnalysisPick & {
  horizon_days: number;
  llm_fallback: number | boolean;
};

export type ThreeDayAnalysisResponse = {
  analysisDate: string | null;
  tradeDate: string | null;
  horizonDays: number;
  llmFallback: boolean;
  candidateCount: number;
  data: ThreeDayAnalysisPick[];
};

export type ThreeDayRunResult = {
  analysisDate: string;
  tradeDate: string;
  horizonDays: number;
  preselectCount: number;
  finalCount: number;
  llmFallback: boolean;
  data: ThreeDayAnalysisPick[];
};

export type StockQuery = {
  current?: number;
  pageSize?: number;
  keyword?: string;
  exchange?: string;
  risk?: string;
};

// 统一处理 API 响应，便于前端组件复用。
async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let detail = `API 请求失败: ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // 响应体不是 JSON 时保留状态码信息。
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

// 读取工作台概览指标。
export function fetchOverview() {
  return requestJson<Overview>('/api/overview');
}

// 读取股票基础信息分页列表。
export function fetchStocks(query: StockQuery) {
  const params = new URLSearchParams();
  params.set('page', String(query.current || 1));
  params.set('page_size', String(query.pageSize || 20));
  if (query.keyword) params.set('keyword', query.keyword);
  if (query.exchange) params.set('exchange', query.exchange);
  if (query.risk) params.set('risk', query.risk);
  return requestJson<{ data: StockRow[]; total: number }>(`/api/stocks?${params}`);
}

// 读取分段统计数据。
export function fetchSegments() {
  return requestJson<Segments>('/api/segments');
}

// 读取单只股票历史行情。
export function fetchHistory(symbol: string, days = 120) {
  return requestJson<HistoryResponse>(`/api/history/${encodeURIComponent(symbol)}?days=${days}`);
}

// 读取最新 T+1 分析结果。
export function fetchAnalysis() {
  return requestJson<AnalysisResponse>('/api/analysis');
}

// 读取最新未来 3 个交易日涨势分析结果。
export function fetchThreeDayAnalysis() {
  return requestJson<ThreeDayAnalysisResponse>('/api/three-day-analysis');
}

// 手动触发未来 3 个交易日涨势分析。
export function runThreeDayAnalysis(finalLimit = 20, useLlm = true) {
  return requestJson<ThreeDayRunResult>('/api/three-day-analysis/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ final_limit: finalLimit, use_llm: useLlm }),
  });
}

// 读取实时分析和模拟交易工作台。
export function fetchRealtimeDashboard() {
  return requestJson<RealtimeDashboard>('/api/realtime');
}

// 触发一次实时分析和模拟交易。
export function runRealtimeAnalysis(
  limit?: number,
  executeTrades = true,
  decisionMode: RealtimeDecisionMode = 'llm_review',
) {
  return requestJson<RealtimeRunResult>('/api/realtime/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ limit, execute_trades: executeTrades, decision_mode: decisionMode }),
  });
}

// 读取集合竞价 LLM 快速选股看板。
export function fetchCallAuctionDashboard(finalLimit = 20) {
  return requestJson<CallAuctionDashboard>(`/api/call-auction?finalLimit=${finalLimit}`);
}

// 执行一次集合竞价 LLM 快速选股。
export function runCallAuction(finalLimit = 5, force = false) {
  return requestJson<CallAuctionRun>('/api/call-auction/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ finalLimit, force }),
  });
}

// 重置模拟交易账户。
export function resetSimulation(initialCash?: number) {
  return requestJson<RealtimeAccount>('/api/simulation/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initial_cash: initialCash }),
  });
}
