import {
  ClockCircleOutlined,
  DashboardOutlined,
  DeleteOutlined,
  FundProjectionScreenOutlined,
  LineChartOutlined,
  ProfileOutlined,
  ReloadOutlined,
  RetweetOutlined,
  RobotOutlined,
  StockOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import {
  PageContainer,
  ProCard,
  ProLayout,
  ProTable,
  StatisticCard,
  type ProColumns,
} from '@ant-design/pro-components';
import { Alert, App as AntdApp, Badge, Button, ConfigProvider, Input, Row, Col, Select, Space, Switch, Tag, Typography } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import gsap from 'gsap';
import { useGSAP } from '@gsap/react';
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  type AnalysisPick,
  type AnalysisResponse,
  type AmountTopItem,
  type HistoryResponse,
  type Overview,
  type RealtimeDashboard,
  type RealtimeDecisionMode,
  type RealtimeSignal,
  type Segments,
  type SimulationOrder,
  type SimulationPosition,
  type StockRow,
  type ThreeDayAnalysisPick,
  type ThreeDayAnalysisResponse,
  fetchAnalysis,
  fetchHistory,
  fetchOverview,
  fetchRealtimeDashboard,
  fetchSegments,
  fetchStocks,
  fetchThreeDayAnalysis,
  resetSimulation,
  runRealtimeAnalysis,
  runThreeDayAnalysis,
} from './api';
import { T1QualitySection } from './T1QualitySection';
import { T1TradingSection } from './T1TradingSection';
import { CallAuctionSection } from './CallAuctionSection';
import { AntvChart } from './components/AntvChart';

const { Statistic } = StatisticCard;

// 判断用户系统是否要求减少动态效果。
function shouldReduceMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// 页面切换动效容器，使用 GSAP 作用域避免影响其他页面节点。
function AnimatedRoutePane({ routeKey, children }: { routeKey: string; children: ReactNode }) {
  const routeRef = useRef<HTMLDivElement | null>(null);

  useGSAP(() => {
    const routeNode = routeRef.current;
    if (!routeNode || shouldReduceMotion()) return;

    const blocks = Array.from(
      routeNode.querySelectorAll('.ant-pro-card, .ant-card, .ant-alert, .ant-pro-table'),
    );
    gsap.fromTo(
      routeNode,
      { autoAlpha: 0, y: 8, filter: 'blur(3px)' },
      { autoAlpha: 1, y: 0, filter: 'blur(0px)', duration: 0.28, ease: 'power2.out', overwrite: 'auto' },
    );
    if (blocks.length > 0) {
      gsap.fromTo(
        blocks,
        { autoAlpha: 0, y: 10 },
        { autoAlpha: 1, y: 0, duration: 0.34, ease: 'power2.out', stagger: 0.035, overwrite: 'auto' },
      );
    }
  }, { scope: routeRef, dependencies: [routeKey], revertOnUpdate: true });

  return (
    <div ref={routeRef} className="animated-route-pane">
      {children}
    </div>
  );
}

const financeTheme = {
  token: {
    colorPrimary: '#0f766e',
    colorInfo: '#0e7490',
    colorSuccess: '#15803d',
    colorWarning: '#b7791f',
    colorError: '#c2410c',
    colorText: '#172033',
    colorTextSecondary: '#5b6472',
    colorBgLayout: '#f4f6f4',
    colorBgContainer: '#ffffff',
    colorBorder: '#d9dfd7',
    borderRadius: 6,
    fontSize: 13,
    controlHeight: 32,
    boxShadowTertiary: '0 1px 2px rgba(16, 24, 40, 0.06)',
  },
  components: {
    Button: {
      borderRadius: 6,
      controlHeight: 32,
      primaryShadow: 'none',
    },
    Card: {
      borderRadiusLG: 6,
      paddingLG: 16,
    },
    Table: {
      cellFontSize: 12,
      cellPaddingBlock: 8,
      cellPaddingInline: 10,
      headerBg: '#f1f4ef',
      headerColor: '#344054',
      rowHoverBg: '#f6faf7',
    },
    Tag: {
      borderRadiusSM: 4,
    },
  },
};

// A 股数据工作台主页面。
export default function App() {
  return (
    <ConfigProvider locale={zhCN} theme={financeTheme}>
      <AntdApp>
        <DashboardApp />
      </AntdApp>
    </ConfigProvider>
  );
}

// 数据工作台内部页面，使用 antd App 上下文弹出反馈。
function DashboardApp() {
  const { message, modal } = AntdApp.useApp();
  const [pathname, setPathname] = useState(() => normalizePathname(window.location.pathname));
  const [overview, setOverview] = useState<Overview | null>(null);
  const [segments, setSegments] = useState<Segments | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [threeDayAnalysis, setThreeDayAnalysis] = useState<ThreeDayAnalysisResponse | null>(null);
  const [realtime, setRealtime] = useState<RealtimeDashboard | null>(null);
  const [historySymbol, setHistorySymbol] = useState('000001');
  const [dataError, setDataError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [threeDayLoading, setThreeDayLoading] = useState(false);
  const [realtimeLoading, setRealtimeLoading] = useState(false);
  const [autoTaskRunning, setAutoTaskRunning] = useState(false);
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(false);
  const [autoTradeEnabled, setAutoTradeEnabled] = useState(false);
  const [refreshIntervalSec, setRefreshIntervalSec] = useState(60);
  const [decisionMode, setDecisionMode] = useState<RealtimeDecisionMode>('llm_review');
  const [lastRealtimeRefreshAt, setLastRealtimeRefreshAt] = useState<string | null>(null);
  const [nextRealtimeRefreshAt, setNextRealtimeRefreshAt] = useState<string | null>(null);
  const [countdownSec, setCountdownSec] = useState(0);
  const [autoTaskError, setAutoTaskError] = useState<string | null>(null);
  const [autoSkippedCount, setAutoSkippedCount] = useState(0);
  const realtimeRequestInFlightRef = useRef(false);

  useEffect(() => {
    const handlePopState = () => setPathname(normalizePathname(window.location.pathname));
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  const loadStaticData = useCallback(async () => {
    setLoading(true);
    setDataError(null);
    const failures: string[] = [];
    try {
      try {
        setOverview(await fetchOverview());
      } catch {
        failures.push('概览');
      }
      try {
        setSegments(await fetchSegments());
      } catch {
        failures.push('分段');
      }
      try {
        setAnalysis(await fetchAnalysis());
      } catch {
        failures.push('分析');
      }
      try {
        setThreeDayAnalysis(await fetchThreeDayAnalysis());
      } catch {
        failures.push('3日分析');
      }
      try {
        setRealtime(await fetchRealtimeDashboard());
      } catch {
        failures.push('实时交易');
      }
      try {
        setHistory(await fetchHistory(historySymbol));
      } catch {
        setHistory(null);
        failures.push('历史');
      }
      if (failures.length > 0) {
        setDataError(`以下数据接口暂不可用：${failures.join('、')}`);
        message.warning('部分数据接口暂不可用，页面已加载可用数据');
      } else {
        setDataError(null);
      }
    } catch (error) {
      setDataError('数据接口加载失败');
      message.error(error instanceof Error ? error.message : '加载数据失败');
    } finally {
      setLoading(false);
    }
  }, [historySymbol, message]);

  const loadRealtimeData = useCallback(async (silent = false) => {
    if (realtimeRequestInFlightRef.current) {
      setAutoSkippedCount((value) => value + 1);
      return;
    }
    realtimeRequestInFlightRef.current = true;
    if (silent) {
      setAutoTaskRunning(true);
    } else {
      setRealtimeLoading(true);
    }
    try {
      setRealtime(await fetchRealtimeDashboard());
      setLastRealtimeRefreshAt(formatDateTime(new Date()));
      setAutoTaskError(null);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '实时交易数据加载失败';
      setAutoTaskError(errorMessage);
      if (!silent) message.error(errorMessage);
    } finally {
      realtimeRequestInFlightRef.current = false;
      if (silent) {
        setAutoTaskRunning(false);
      } else {
        setRealtimeLoading(false);
      }
    }
  }, [message]);

  const handleRunRealtime = useCallback(async (executeTrades = true, silent = false) => {
    if (realtimeRequestInFlightRef.current) {
      setAutoSkippedCount((value) => value + 1);
      if (!silent) message.warning('上一轮实时任务仍在运行，已跳过本次请求');
      return;
    }
    realtimeRequestInFlightRef.current = true;
    if (silent) {
      setAutoTaskRunning(true);
    } else {
      setRealtimeLoading(true);
    }
    try {
      const result = await runRealtimeAnalysis(undefined, executeTrades, decisionMode);
      if (!silent) {
        if (result.skipped) {
          message.warning(result.skipReason || '非开盘时间，已跳过实时分析和模拟交易');
        } else {
          const llmText = result.llmFallback ? '，LLM 已降级为规则' : '';
          message.success(`实时分析完成：信号 ${result.signalCount} 条，订单 ${result.orderCount} 笔${llmText}`);
        }
      }
      setRealtime(await fetchRealtimeDashboard());
      setOverview(await fetchOverview());
      setLastRealtimeRefreshAt(formatDateTime(new Date()));
      setAutoTaskError(result.skipped ? result.skipReason : null);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '实时分析执行失败';
      setAutoTaskError(errorMessage);
      if (!silent) message.error(errorMessage);
    } finally {
      realtimeRequestInFlightRef.current = false;
      if (silent) {
        setAutoTaskRunning(false);
      } else {
        setRealtimeLoading(false);
      }
    }
  }, [decisionMode, message]);

  const handleResetSimulation = useCallback(() => {
    modal.confirm({
      title: '重置模拟账户',
      content: '当前持仓、订单和成交记录会被清空。',
      okText: '重置',
      cancelText: '取消',
      onOk: async () => {
        setRealtimeLoading(true);
        try {
          await resetSimulation();
          message.success('模拟账户已重置');
          setRealtime(await fetchRealtimeDashboard());
        } catch (error) {
          message.error(error instanceof Error ? error.message : '模拟账户重置失败');
        } finally {
          setRealtimeLoading(false);
        }
      },
    });
  }, [message, modal]);

  const handleRunThreeDayAnalysis = useCallback(async () => {
    setThreeDayLoading(true);
    try {
      const result = await runThreeDayAnalysis(20, true);
      const fallbackText = result.llmFallback ? '，LLM 已降级为量化排序' : '';
      message.success(`3日分析完成：预筛 ${result.preselectCount} 只，输出 ${result.finalCount} 只${fallbackText}`);
      setThreeDayAnalysis(await fetchThreeDayAnalysis());
      setOverview(await fetchOverview());
    } catch (error) {
      message.error(error instanceof Error ? error.message : '3日分析执行失败');
    } finally {
      setThreeDayLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void loadStaticData();
  }, [loadStaticData]);

  useEffect(() => {
    if (pathname !== '/realtime' || !autoRefreshEnabled) {
      setCountdownSec(0);
      setNextRealtimeRefreshAt(null);
      return undefined;
    }

    const updateNextTime = () => {
      setCountdownSec(refreshIntervalSec);
      setNextRealtimeRefreshAt(formatDateTime(new Date(Date.now() + refreshIntervalSec * 1000)));
    };

    updateNextTime();
    const timer = window.setInterval(() => {
      setCountdownSec((value) => {
        if (value > 1) return value - 1;
        if (autoTradeEnabled && realtime?.marketStatus?.marketOpen !== false) {
          void handleRunRealtime(true, true);
        } else {
          void loadRealtimeData(true);
        }
        setNextRealtimeRefreshAt(formatDateTime(new Date(Date.now() + refreshIntervalSec * 1000)));
        return refreshIntervalSec;
      });
    }, 1000);

    return () => window.clearInterval(timer);
  }, [
    autoRefreshEnabled,
    autoTradeEnabled,
    handleRunRealtime,
    loadRealtimeData,
    pathname,
    realtime?.marketStatus?.marketOpen,
    refreshIntervalSec,
  ]);

  const stockColumns = useMemo<ProColumns<StockRow>[]>(
    () => [
      {
        title: '关键词',
        dataIndex: 'keyword',
        hideInTable: true,
      },
      {
        title: '代码',
        dataIndex: 'symbol',
        width: 96,
        copyable: true,
        search: false,
      },
      {
        title: '名称',
        dataIndex: 'name',
        width: 120,
        search: false,
      },
      {
        title: '交易所',
        dataIndex: 'exchange',
        width: 88,
        valueEnum: {
          SH: { text: '沪市' },
          SZ: { text: '深市' },
          BJ: { text: '北交所' },
        },
      },
      {
        title: '风险',
        dataIndex: 'risk',
        width: 92,
        valueEnum: {
          normal: { text: '普通股' },
          risk: { text: 'ST/退市' },
        },
        render: (_, row) => (row.is_risk ? <Tag color="red">风险</Tag> : <Tag color="green">普通</Tag>),
      },
      {
        title: '最新收盘',
        dataIndex: 'close_price',
        width: 110,
        search: false,
        renderText: formatPrice,
      },
      {
        title: '涨跌幅',
        dataIndex: 'pct_change',
        width: 100,
        search: false,
        render: (_, row) => <PriceChange value={row.pct_change} />,
      },
      {
        title: '换手率',
        dataIndex: 'turnover_rate',
        width: 100,
        search: false,
        renderText: formatPercent,
      },
      {
        title: '成交额',
        dataIndex: 'amount',
        width: 120,
        search: false,
        renderText: formatAmount,
      },
      {
        title: '操作',
        valueType: 'option',
        width: 100,
        render: (_, row) => [
          <Button
            key="history"
            size="small"
            type="link"
            onClick={() => {
              setHistorySymbol(row.symbol);
              setPathname('/history');
            }}
          >
            历史
          </Button>,
        ],
      },
    ],
    [],
  );

  const analysisColumns = useMemo<ProColumns<AnalysisPick>[]>(
    () => [
      { title: '排名', dataIndex: 'rank_no', width: 72, search: false },
      { title: '代码', dataIndex: 'symbol', width: 96, copyable: true, search: false },
      { title: '名称', dataIndex: 'name', width: 120, search: false },
      { title: '综合评分', dataIndex: 'final_score', width: 110, search: false, renderText: formatScore },
      { title: '量化评分', dataIndex: 'quant_score', width: 110, search: false, renderText: formatScore },
      { title: 'LLM评分', dataIndex: 'llm_score', width: 110, search: false, renderText: formatScore },
      { title: '理由', dataIndex: 'reason', search: false },
      { title: '风险', dataIndex: 'risk', search: false },
    ],
    [],
  );

  const threeDayColumns = useMemo<ProColumns<ThreeDayAnalysisPick>[]>(
    () => [
      { title: '排名', dataIndex: 'rank_no', width: 72, search: false },
      { title: '代码', dataIndex: 'symbol', width: 96, copyable: true, search: false },
      { title: '名称', dataIndex: 'name', width: 120, search: false },
      { title: '综合评分', dataIndex: 'final_score', width: 110, search: false, renderText: formatScore },
      { title: '量化评分', dataIndex: 'quant_score', width: 110, search: false, renderText: formatScore },
      { title: 'LLM评分', dataIndex: 'llm_score', width: 110, search: false, renderText: formatScore },
      { title: '预期方向', dataIndex: 'expected_direction', width: 150, search: false },
      {
        title: '理由',
        dataIndex: 'reason',
        width: 260,
        search: false,
        render: (_, row) => <Typography.Text ellipsis>{row.reason}</Typography.Text>,
      },
      {
        title: '风险',
        dataIndex: 'risk',
        width: 220,
        search: false,
        render: (_, row) => <Typography.Text ellipsis>{row.risk}</Typography.Text>,
      },
    ],
    [],
  );

  const signalColumns = useMemo<ProColumns<RealtimeSignal>[]>(
    () => [
      { title: '动作', dataIndex: 'signal_action', width: 80, render: (_, row) => <ActionTag action={row.signal_action} /> },
      { title: '代码', dataIndex: 'symbol', width: 96, copyable: true },
      { title: '名称', dataIndex: 'name', width: 120 },
      { title: '现价', dataIndex: 'latest_price', width: 92, renderText: formatPrice },
      { title: '涨跌幅', dataIndex: 'pct_change', width: 92, render: (_, row) => <PriceChange value={row.pct_change} /> },
      { title: '综合', dataIndex: 'final_score', width: 88, renderText: formatScore },
      { title: '趋势', dataIndex: 'trend_score', width: 88, renderText: formatScore },
      { title: '动量', dataIndex: 'momentum_score', width: 88, renderText: formatScore },
      { title: '决策源', dataIndex: 'decision_source', width: 96, render: (_, row) => <DecisionSourceTag row={row} /> },
      { title: 'LLM分', dataIndex: 'llm_score', width: 88, renderText: formatScore },
      {
        title: 'LLM理由',
        dataIndex: 'llm_reason',
        width: 180,
        render: (_, row) => <Typography.Text ellipsis>{row.llm_reason || '-'}</Typography.Text>,
      },
      { title: '理由', dataIndex: 'reason' },
      { title: '风险', dataIndex: 'risk' },
    ],
    [],
  );

  const positionColumns = useMemo<ProColumns<SimulationPosition>[]>(
    () => [
      { title: '代码', dataIndex: 'symbol', width: 96, copyable: true },
      { title: '名称', dataIndex: 'name', width: 120 },
      { title: '持仓', dataIndex: 'quantity', width: 96, renderText: formatQuantity },
      { title: '成本', dataIndex: 'avg_cost', width: 92, renderText: formatPrice },
      { title: '现价', dataIndex: 'last_price', width: 92, renderText: formatPrice },
      { title: '市值', dataIndex: 'market_value', width: 120, renderText: formatCurrency },
      { title: '浮盈', dataIndex: 'floating_pnl', width: 120, render: (_, row) => <MoneyChange value={row.floating_pnl} /> },
    ],
    [],
  );

  const orderColumns = useMemo<ProColumns<SimulationOrder>[]>(
    () => [
      { title: '时间', dataIndex: 'order_time', width: 170 },
      { title: '方向', dataIndex: 'side', width: 76, render: (_, row) => <SideTag side={row.side} /> },
      { title: '代码', dataIndex: 'symbol', width: 96, copyable: true },
      { title: '名称', dataIndex: 'name', width: 120 },
      { title: '数量', dataIndex: 'quantity', width: 96, renderText: formatQuantity },
      { title: '价格', dataIndex: 'price', width: 90, renderText: formatPrice },
      { title: '金额', dataIndex: 'amount', width: 120, renderText: formatCurrency },
      { title: '原因', dataIndex: 'reason' },
    ],
    [],
  );

  return (
    <ProLayout
        className="finance-layout"
        title="A股量化交易台"
        logo={false}
        layout="mix"
        colorPrimary="#0f766e"
        siderWidth={216}
        location={{ pathname }}
        menuDataRender={() => [
          { path: '/overview', name: '数据概览', icon: <DashboardOutlined /> },
          { path: '/stocks', name: '基础信息', icon: <StockOutlined /> },
          { path: '/segments', name: '分段数据', icon: <FundProjectionScreenOutlined /> },
          { path: '/history', name: '历史数据', icon: <LineChartOutlined /> },
          { path: '/analysis', name: '分析数据', icon: <ProfileOutlined /> },
          { path: '/three-day-analysis', name: '3日分析', icon: <LineChartOutlined /> },
          { path: '/call-auction', name: '集合竞价', icon: <ClockCircleOutlined /> },
          { path: '/realtime', name: '实时交易', icon: <ThunderboltOutlined /> },
          { path: '/t1-trading', name: 'T+1交易', icon: <RetweetOutlined /> },
          { path: '/t1-quality', name: '14:05质量选股', icon: <ClockCircleOutlined /> },
        ]}
        menuItemRender={(item, dom) => (
          <button
            className="menu-button"
            onClick={() => {
              const nextPathname = normalizePathname(item.path || '/overview');
              setPathname(nextPathname);
              window.history.pushState(null, '', nextPathname);
            }}
            type="button"
          >
            {dom}
          </button>
        )}
        token={{
          header: {
            colorBgHeader: '#ffffff',
            colorHeaderTitle: '#172033',
            colorTextMenu: '#344054',
          },
          sider: {
            colorMenuBackground: '#07111f',
            colorTextMenu: '#c9d4df',
            colorTextMenuSelected: '#f8fafc',
            colorBgMenuItemSelected: '#0f766e',
            colorBgMenuItemHover: '#102033',
          },
        }}
      >
        <PageContainer
          className="finance-page"
          title={pageTitle(pathname)}
          subTitle={`最新交易日 ${overview?.latest_trade_date || '-'}`}
          loading={loading && !overview}
          extra={
            <Button onClick={() => void loadStaticData()} type="primary">
              刷新
            </Button>
          }
        >
          <AnimatedRoutePane routeKey={pathname}>
            <Space direction="vertical" size={16} className="page-stack">
              {dataError && <Alert showIcon type="warning" message={dataError} />}
              {pathname === '/overview' && (
                <OverviewSection overview={overview} analysis={analysis} threeDayAnalysis={threeDayAnalysis} />
              )}
              {pathname === '/stocks' && <StocksSection columns={stockColumns} />}
              {pathname === '/segments' && <SegmentsSection segments={segments} />}
              {pathname === '/history' && (
                <HistorySection
                  history={history}
                  symbol={historySymbol}
                  onSymbolChange={async (symbol) => {
                    const normalizedSymbol = symbol.trim();
                    if (!normalizedSymbol) {
                      message.warning('请输入股票代码');
                      return;
                    }
                    setHistorySymbol(normalizedSymbol);
                    setLoading(true);
                    try {
                      setHistory(await fetchHistory(normalizedSymbol));
                    } catch (error) {
                      setHistory(null);
                      message.error(error instanceof Error ? error.message : '历史数据加载失败');
                    } finally {
                      setLoading(false);
                    }
                  }}
                />
              )}
              {pathname === '/analysis' && (
                <AnalysisSection analysis={analysis} columns={analysisColumns} />
              )}
              {pathname === '/three-day-analysis' && (
                <ThreeDayAnalysisSection
                  analysis={threeDayAnalysis}
                  columns={threeDayColumns}
                  loading={threeDayLoading}
                  onRun={() => void handleRunThreeDayAnalysis()}
                  onRefresh={async () => {
                    setThreeDayLoading(true);
                    try {
                      setThreeDayAnalysis(await fetchThreeDayAnalysis());
                    } catch (error) {
                      message.error(error instanceof Error ? error.message : '3日分析刷新失败');
                    } finally {
                      setThreeDayLoading(false);
                    }
                  }}
                />
              )}
              {pathname === '/call-auction' && <CallAuctionSection />}
              {pathname === '/realtime' && (
                <RealtimeSection
                  realtime={realtime}
                  loading={realtimeLoading}
                  autoTaskRunning={autoTaskRunning}
                  autoRefreshEnabled={autoRefreshEnabled}
                  autoTradeEnabled={autoTradeEnabled}
                  refreshIntervalSec={refreshIntervalSec}
                  decisionMode={decisionMode}
                  lastRealtimeRefreshAt={lastRealtimeRefreshAt}
                  nextRealtimeRefreshAt={nextRealtimeRefreshAt}
                  countdownSec={countdownSec}
                  autoTaskError={autoTaskError}
                  autoSkippedCount={autoSkippedCount}
                  signalColumns={signalColumns}
                  positionColumns={positionColumns}
                  orderColumns={orderColumns}
                  onRefresh={() => void loadRealtimeData()}
                  onRun={() => void handleRunRealtime(true)}
                  onReset={handleResetSimulation}
                  onAutoRefreshChange={(checked) => {
                    setAutoRefreshEnabled(checked);
                    if (!checked) setAutoTradeEnabled(false);
                  }}
                  onAutoTradeChange={setAutoTradeEnabled}
                  onIntervalChange={setRefreshIntervalSec}
                  onDecisionModeChange={setDecisionMode}
                />
              )}
              {pathname === '/t1-trading' && <T1TradingSection />}
              {pathname === '/t1-quality' && <T1QualitySection />}
            </Space>
          </AnimatedRoutePane>
        </PageContainer>
      </ProLayout>
  );
}

// 概览区块展示核心数据规模和最新分析结果。
function OverviewSection({
  overview,
  analysis,
  threeDayAnalysis,
}: {
  overview: Overview | null;
  analysis: AnalysisResponse | null;
  threeDayAnalysis: ThreeDayAnalysisResponse | null;
}) {
  return (
    <>
      <StatisticCard.Group direction="row">
        <StatisticCard title="股票总数" statistic={{ value: overview?.stock_count ?? '-', suffix: overview ? '只' : undefined }} />
        <StatisticCard title="最新日线覆盖" statistic={{ value: overview?.latest_symbol_count ?? '-', suffix: overview ? '只' : undefined }} />
        <StatisticCard title="历史行情" statistic={{ value: overview?.daily_count ?? '-', suffix: overview ? '行' : undefined }} />
        <StatisticCard title="T+1候选" statistic={{ value: overview?.analysis_count ?? '-', suffix: overview ? '只' : undefined }} />
        <StatisticCard title="3日候选" statistic={{ value: overview?.three_day_analysis_count ?? '-', suffix: overview ? '只' : undefined }} />
      </StatisticCard.Group>
      <ProCard title="最新 T+1 候选" bordered headerBordered>
        <Row gutter={[12, 12]}>
          {(analysis?.data || []).slice(0, 5).map((item) => (
            <Col xs={24} md={12} xl={8} key={item.symbol}>
              <div className="pick-tile">
                <div>
                  <Typography.Text strong>{item.rank_no}. {item.name}</Typography.Text>
                  <Typography.Text className="muted"> {item.symbol}</Typography.Text>
                </div>
                <div className="pick-score">{formatScore(item.final_score)}</div>
                <Typography.Paragraph className="pick-reason" ellipsis={{ rows: 2 }}>
                  {item.reason}
                </Typography.Paragraph>
              </div>
            </Col>
          ))}
        </Row>
      </ProCard>
      <ProCard title="最新 3 日候选" bordered headerBordered>
        <Row gutter={[12, 12]}>
          {(threeDayAnalysis?.data || []).slice(0, 5).map((item) => (
            <Col xs={24} md={12} xl={8} key={item.symbol}>
              <div className="pick-tile">
                <div>
                  <Typography.Text strong>{item.rank_no}. {item.name}</Typography.Text>
                  <Typography.Text className="muted"> {item.symbol}</Typography.Text>
                </div>
                <div className="pick-score">{formatScore(item.final_score)}</div>
                <Typography.Paragraph className="pick-reason" ellipsis={{ rows: 2 }}>
                  {item.reason}
                </Typography.Paragraph>
              </div>
            </Col>
          ))}
        </Row>
      </ProCard>
    </>
  );
}

// 基础信息区块使用 ProTable 查询股票池。
function StocksSection({ columns }: { columns: ProColumns<StockRow>[] }) {
  return (
    <ProTable<StockRow>
      rowKey="symbol"
      columns={columns}
      cardBordered
      pagination={{ pageSize: 20 }}
      request={async (params) => {
        const result = await fetchStocks(params);
        return { data: result.data, success: true, total: result.total };
      }}
      search={{ labelWidth: 72 }}
      options={false}
      scroll={{ x: 980 }}
    />
  );
}

// 分段数据区块展示交易所、风险、涨跌幅和成交额排行。
function SegmentsSection({ segments }: { segments: Segments | null }) {
  const amountTop = (segments?.amountTop || [])
    .filter((item: AmountTopItem) => typeof item.amount === 'number')
    .map((item: AmountTopItem) => ({
      name: `${item.symbol} ${item.name}`,
      amountYi: Number(((item.amount || 0) / 100000000).toFixed(2)),
      pct_change: item.pct_change,
    }));

  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} lg={12}>
        <ProCard title="交易所分布" bordered headerBordered>
          <AntvChart
            type="pie"
            options={{
              data: segments?.exchangeDistribution || [],
              angleField: 'value',
              colorField: 'name',
              radius: 0.82,
              label: { type: 'outer', content: '{name} {percentage}' },
            }}
          />
        </ProCard>
      </Col>
      <Col xs={24} lg={12}>
        <ProCard title="ST 与普通股分布" bordered headerBordered>
          <AntvChart
            type="pie"
            options={{
              data: segments?.riskDistribution || [],
              angleField: 'value',
              colorField: 'name',
              radius: 0.82,
              label: { type: 'outer', content: '{name} {percentage}' },
            }}
          />
        </ProCard>
      </Col>
      <Col xs={24} lg={12}>
        <ProCard title="最新涨跌幅分段" bordered headerBordered>
          <AntvChart
            type="column"
            options={{
              data: segments?.pctDistribution || [],
              xField: 'name',
              yField: 'value',
              color: '#0f766e',
            }}
          />
        </ProCard>
      </Col>
      <Col xs={24} lg={12}>
        <ProCard title="成交额 Top 15" bordered headerBordered>
          <AntvChart
            type="column"
            options={{
              data: amountTop,
              xField: 'name',
              yField: 'amountYi',
              color: '#0e7490',
              xAxis: { label: { autoRotate: true, autoHide: true } },
              meta: { amountYi: { alias: '成交额(亿元)' } },
            }}
          />
        </ProCard>
      </Col>
    </Row>
  );
}

// 历史数据区块展示单只股票收盘价和成交量。
function HistorySection({
  history,
  symbol,
  onSymbolChange,
}: {
  history: HistoryResponse | null;
  symbol: string;
  onSymbolChange: (symbol: string) => Promise<void>;
}) {
  const closeData = (history?.data || [])
    .filter((item) => typeof item.close_price === 'number')
    .map((item) => ({
      date: item.trade_date,
      close: item.close_price,
    }));
  const volumeData = (history?.data || [])
    .filter((item) => typeof item.volume === 'number')
    .map((item) => ({
      date: item.trade_date,
      volume: Number(((item.volume || 0) / 10000).toFixed(2)),
    }));

  return (
    <Space direction="vertical" size={16} className="page-stack">
      <ProCard bordered>
        <Space wrap>
          <Input.Search
            key={symbol}
            defaultValue={symbol}
            enterButton="加载"
            onSearch={(value) => void onSymbolChange(value)}
            placeholder="输入股票代码，如 000001"
            style={{ width: 280 }}
          />
          <Typography.Text strong>
            {history?.stock?.name || '-'} {history?.stock?.symbol || ''}
          </Typography.Text>
          <Tag>{history?.stock?.exchange || '-'}</Tag>
        </Space>
      </ProCard>
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <ProCard title="收盘价走势" bordered headerBordered>
            <AntvChart
              type="line"
              options={{
                data: closeData,
                xField: 'date',
                yField: 'close',
                smooth: true,
                color: '#0f766e',
              }}
            />
          </ProCard>
        </Col>
        <Col xs={24} lg={10}>
          <ProCard title="成交量" bordered headerBordered>
            <AntvChart
              type="column"
              options={{
                data: volumeData,
                xField: 'date',
                yField: 'volume',
                color: '#b7791f',
                meta: { volume: { alias: '成交量(万手)' } },
              }}
            />
          </ProCard>
        </Col>
      </Row>
    </Space>
  );
}

// 分析数据区块展示 T+1 大模型选股结果。
function AnalysisSection({
  analysis,
  columns,
}: {
  analysis: AnalysisResponse | null;
  columns: ProColumns<AnalysisPick>[];
}) {
  return (
    <ProTable<AnalysisPick>
      rowKey="symbol"
      columns={columns}
      dataSource={analysis?.data || []}
      search={false}
      pagination={false}
      cardBordered
      headerTitle={`分析日期 ${analysis?.analysisDate || '-'}`}
      scroll={{ x: 980 }}
    />
  );
}

// 3 日分析区块展示全 A 股短线涨势候选。
function ThreeDayAnalysisSection({
  analysis,
  columns,
  loading,
  onRun,
  onRefresh,
}: {
  analysis: ThreeDayAnalysisResponse | null;
  columns: ProColumns<ThreeDayAnalysisPick>[];
  loading: boolean;
  onRun: () => void;
  onRefresh: () => void;
}) {
  return (
    <Space direction="vertical" size={16} className="page-stack">
      <ProCard bordered>
        <Space direction="vertical" size={12} className="page-stack">
          <Space wrap>
            <Button type="primary" icon={<ThunderboltOutlined />} loading={loading} onClick={onRun}>
              运行3日分析
            </Button>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={onRefresh}>
              刷新
            </Button>
            <Tag color="blue">未来3个交易日</Tag>
            <Tag color={analysis?.llmFallback ? 'gold' : 'purple'}>
              {analysis?.llmFallback ? 'LLM降级' : 'LLM复核'}
            </Tag>
          </Space>
          <Space wrap>
            <Typography.Text className="muted">分析日期 {analysis?.analysisDate || '-'}</Typography.Text>
            <Typography.Text className="muted">基准交易日 {analysis?.tradeDate || '-'}</Typography.Text>
            <Typography.Text className="muted">候选数量 {analysis?.candidateCount ?? 0} 只</Typography.Text>
          </Space>
          <Alert
            showIcon
            type="info"
            message="结果用于策略研究和模拟观察，不构成投资建议。"
          />
        </Space>
      </ProCard>
      <ProTable<ThreeDayAnalysisPick>
        rowKey="symbol"
        columns={columns}
        dataSource={analysis?.data || []}
        search={false}
        pagination={{ pageSize: 20 }}
        cardBordered
        headerTitle="全A股未来3个交易日涨势候选"
        options={false}
        loading={loading}
        scroll={{ x: 1240 }}
      />
    </Space>
  );
}

// 实时交易区块展示信号、账户、持仓和订单。
function RealtimeSection({
  realtime,
  loading,
  autoTaskRunning,
  autoRefreshEnabled,
  autoTradeEnabled,
  refreshIntervalSec,
  decisionMode,
  lastRealtimeRefreshAt,
  nextRealtimeRefreshAt,
  countdownSec,
  autoTaskError,
  autoSkippedCount,
  signalColumns,
  positionColumns,
  orderColumns,
  onRefresh,
  onRun,
  onReset,
  onAutoRefreshChange,
  onAutoTradeChange,
  onIntervalChange,
  onDecisionModeChange,
}: {
  realtime: RealtimeDashboard | null;
  loading: boolean;
  autoTaskRunning: boolean;
  autoRefreshEnabled: boolean;
  autoTradeEnabled: boolean;
  refreshIntervalSec: number;
  decisionMode: RealtimeDecisionMode;
  lastRealtimeRefreshAt: string | null;
  nextRealtimeRefreshAt: string | null;
  countdownSec: number;
  autoTaskError: string | null;
  autoSkippedCount: number;
  signalColumns: ProColumns<RealtimeSignal>[];
  positionColumns: ProColumns<SimulationPosition>[];
  orderColumns: ProColumns<SimulationOrder>[];
  onRefresh: () => void;
  onRun: () => void;
  onReset: () => void;
  onAutoRefreshChange: (checked: boolean) => void;
  onAutoTradeChange: (checked: boolean) => void;
  onIntervalChange: (value: number) => void;
  onDecisionModeChange: (value: RealtimeDecisionMode) => void;
}) {
  const account = realtime?.account;
  const totalReturn =
    account && account.initial_cash
      ? ((account.total_asset - account.initial_cash) / account.initial_cash) * 100
      : 0;
  const totalPnl = account ? account.total_asset - account.initial_cash : null;
  const floatingPnl = realtime
    ? realtime.positions.reduce((sum, row) => sum + Number(row.floating_pnl || 0), 0)
    : null;
  const realizedPnl = account ? account.realized_pnl : null;
  const decisionSummary = realtime?.decisionSummary;
  const fallbackCount = Number(decisionSummary?.fallback_count || 0);
  const marketStatus = realtime?.marketStatus;
  const marketOpen = marketStatus?.marketOpen !== false;
  const actionDisabled = Boolean(marketStatus && !marketStatus.marketOpen);

  return (
    <Space direction="vertical" size={16} className="page-stack">
      <ProCard bordered>
        <Space direction="vertical" size={12} className="page-stack">
          <Space wrap>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={loading}
            disabled={actionDisabled}
            onClick={onRun}
          >
            运行分析
          </Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={onRefresh}>
            刷新
          </Button>
          <Button danger icon={<DeleteOutlined />} onClick={onReset}>
            重置模拟
          </Button>
          <Space size={6}>
            <Typography.Text>自动刷新</Typography.Text>
            <Switch checked={autoRefreshEnabled} onChange={onAutoRefreshChange} />
          </Space>
          <Space size={6}>
            <Typography.Text>自动买卖</Typography.Text>
            <Switch
              checked={autoTradeEnabled && marketOpen}
              disabled={!autoRefreshEnabled || actionDisabled}
              onChange={onAutoTradeChange}
            />
          </Space>
          <Select
            aria-label="自动刷新间隔"
            value={refreshIntervalSec}
            style={{ width: 108 }}
            onChange={onIntervalChange}
            options={[
              { value: 15, label: '15秒' },
              { value: 30, label: '30秒' },
              { value: 60, label: '60秒' },
              { value: 120, label: '120秒' },
            ]}
          />
          <Select<RealtimeDecisionMode>
            aria-label="决策模式"
            value={decisionMode}
            style={{ width: 132 }}
            onChange={onDecisionModeChange}
            options={[
              { value: 'llm_review', label: 'LLM复核' },
              { value: 'rules', label: '规则引擎' },
            ]}
          />
          <Badge
            status={autoTaskRunning || loading ? 'processing' : autoRefreshEnabled ? 'success' : 'default'}
            text={
              <span>
                <ClockCircleOutlined />{' '}
                {autoTaskRunning ? '自动任务运行中' : autoRefreshEnabled ? '自动刷新已开启' : '自动刷新未开启'}
              </span>
            }
          />
          <Tag color={marketOpen ? 'green' : 'default'}>
            {marketOpen ? '开盘中' : '非开盘'}
          </Tag>
          </Space>
          <Space wrap>
            <Typography.Text className="muted">
              信号时间 {realtime?.latestSignalTime || '-'}，行情快照 {realtime?.quoteSummary?.snapshot_time || '-'}
            </Typography.Text>
            <Typography.Text className="muted">
              最近刷新 {lastRealtimeRefreshAt || '-'}，下次刷新{' '}
              {autoRefreshEnabled ? `${countdownSec}秒后 (${nextRealtimeRefreshAt || '-'})` : '-'}
            </Typography.Text>
            <Typography.Text className="muted">
              决策 {decisionModeText(decisionMode)}，LLM {decisionSummary?.llm_count || 0} 条，
              降级 {fallbackCount} 条，跳过 {autoSkippedCount} 次
            </Typography.Text>
            <Typography.Text className="muted">
              市场状态 {marketStatus?.reason || '-'}
            </Typography.Text>
          </Space>
          {actionDisabled && (
            <Alert showIcon type="info" message={marketStatus?.reason || '非开盘时间，实时分析和模拟交易暂停'} />
          )}
          {autoTaskError && <Alert showIcon type="warning" message={autoTaskError} />}
          {fallbackCount > 0 && (
            <Alert showIcon type="info" message={`最近一轮有 ${fallbackCount} 条 LLM 决策降级为规则引擎`} />
          )}
        </Space>
      </ProCard>
      <StatisticCard.Group direction="row">
        <StatisticCard title="总资产" statistic={{ value: formatCurrency(account?.total_asset) }} />
        <StatisticCard title="总盈亏" statistic={{ value: formatSignedCurrency(totalPnl), valueStyle: { color: profitColor(totalPnl) } }} />
        <StatisticCard title="可用现金" statistic={{ value: formatCurrency(account?.cash) }} />
        <StatisticCard title="持仓市值" statistic={{ value: formatCurrency(account?.market_value) }} />
        <StatisticCard title="持仓浮盈" statistic={{ value: formatSignedCurrency(floatingPnl), valueStyle: { color: profitColor(floatingPnl) } }} />
        <StatisticCard title="已实现盈亏" statistic={{ value: formatSignedCurrency(realizedPnl), valueStyle: { color: profitColor(realizedPnl) } }} />
        <StatisticCard title="收益率" statistic={{ value: `${totalReturn.toFixed(2)}%`, valueStyle: { color: totalReturn >= 0 ? '#d92d20' : '#039855' } }} />
      </StatisticCard.Group>
      <ProTable<RealtimeSignal>
        rowKey="id"
        columns={signalColumns}
        dataSource={realtime?.signals || []}
        search={false}
        pagination={{ pageSize: 12 }}
        cardBordered
        headerTitle="实时分析信号"
        options={false}
        loading={loading}
        scroll={{ x: 1480 }}
      />
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <ProTable<SimulationPosition>
            rowKey="symbol"
            columns={positionColumns}
            dataSource={realtime?.positions || []}
            search={false}
            pagination={false}
            cardBordered
            headerTitle="模拟持仓"
            options={false}
            loading={loading}
            scroll={{ x: 780 }}
          />
        </Col>
        <Col xs={24} xl={12}>
          <ProTable<SimulationOrder>
            rowKey="id"
            columns={orderColumns}
            dataSource={realtime?.orders || []}
            search={false}
            pagination={{ pageSize: 8 }}
            cardBordered
            headerTitle="模拟订单"
            options={false}
            loading={loading}
            scroll={{ x: 900 }}
          />
        </Col>
      </Row>
    </Space>
  );
}

// 涨跌幅显示组件。
function PriceChange({ value }: { value?: number | null }) {
  const numeric = Number(value || 0);
  const color = numeric > 0 ? '#d92d20' : numeric < 0 ? '#039855' : '#667085';
  return <span style={{ color }}>{formatPercent(numeric)}</span>;
}

// 金额涨跌显示组件。
function MoneyChange({ value }: { value?: number | null }) {
  const numeric = Number(value || 0);
  const color = numeric > 0 ? '#d92d20' : numeric < 0 ? '#039855' : '#667085';
  return <span style={{ color }}>{formatCurrency(numeric)}</span>;
}

// 实时信号标签。
function ActionTag({ action }: { action: RealtimeSignal['signal_action'] }) {
  const config = {
    BUY: { color: 'red', text: '买入' },
    SELL: { color: 'green', text: '卖出' },
    WATCH: { color: 'blue', text: '观察' },
    HOLD: { color: 'default', text: '持有' },
  }[action];
  return <Tag color={config.color}>{config.text}</Tag>;
}

// 实时决策来源标签。
function DecisionSourceTag({ row }: { row: RealtimeSignal }) {
  if (row.llm_fallback) return <Tag color="gold">规则降级</Tag>;
  if (row.decision_source === 'llm') return <Tag color="purple" icon={<RobotOutlined />}>LLM复核</Tag>;
  if (row.decision_source === 'rules') return <Tag>规则</Tag>;
  return <Tag>-</Tag>;
}

// 订单方向标签。
function SideTag({ side }: { side: SimulationOrder['side'] }) {
  return <Tag color={side === 'BUY' ? 'red' : 'green'}>{side === 'BUY' ? '买入' : '卖出'}</Tag>;
}

// 返回决策模式中文名称。
function decisionModeText(mode: RealtimeDecisionMode) {
  return mode === 'llm_review' ? '规则+LLM复核' : '规则引擎';
}

// 返回当前菜单对应页面标题。
function pageTitle(pathname: string) {
  const map: Record<string, string> = {
    '/overview': '数据概览',
    '/stocks': 'A股基础信息',
    '/segments': '分段数据',
    '/history': '历史数据',
    '/analysis': '分析数据',
    '/three-day-analysis': '全A股未来3个交易日涨势分析',
    '/call-auction': '集合竞价LLM快速选股',
    '/realtime': '实时分析与模拟交易',
    '/t1-trading': 'T+1模拟交易',
    '/t1-quality': '14:05盘中质量选股',
  };
  return map[pathname] || '数据概览';
}

// 规范化浏览器路径，避免未知路径导致空白页面。
function normalizePathname(pathname: string) {
  const allowedPathnames = new Set([
    '/overview',
    '/stocks',
    '/segments',
    '/history',
    '/analysis',
    '/three-day-analysis',
    '/call-auction',
    '/realtime',
    '/t1-trading',
    '/t1-quality',
  ]);
  if (!pathname || pathname === '/') return '/overview';
  return allowedPathnames.has(pathname) ? pathname : '/overview';
}

// 格式化价格。
function formatPrice(value?: number | null) {
  return value === undefined || value === null ? '-' : Number(value).toFixed(2);
}

// 格式化百分比。
function formatPercent(value?: number | null) {
  return value === undefined || value === null ? '-' : `${Number(value).toFixed(2)}%`;
}

// 格式化成交额。
function formatAmount(value?: number | null) {
  if (value === undefined || value === null) return '-';
  return `${(Number(value) / 100000000).toFixed(2)}亿`;
}

// 格式化评分。
function formatScore(value?: number | null) {
  return value === undefined || value === null ? '-' : Number(value).toFixed(2);
}

// 格式化货币。
function formatCurrency(value?: number | null) {
  if (value === undefined || value === null) return '-';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

// 格式化带正负号的盈亏金额。
function formatSignedCurrency(value?: number | null) {
  if (value === undefined || value === null) return '-';
  const numeric = Number(value);
  const sign = numeric > 0 ? '+' : '';
  return `${sign}${formatCurrency(numeric)}`;
}

// 返回盈亏金额显示颜色，A 股习惯红涨绿跌。
function profitColor(value?: number | null) {
  const numeric = Number(value || 0);
  return numeric >= 0 ? '#d92d20' : '#039855';
}

// 格式化股数。
function formatQuantity(value?: number | null) {
  if (value === undefined || value === null) return '-';
  return Number(value).toLocaleString('zh-CN');
}

// 格式化本地时间，供自动刷新状态栏展示。
function formatDateTime(value: Date) {
  return value.toLocaleString('zh-CN', { hour12: false });
}
