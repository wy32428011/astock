import {
  ClockCircleOutlined,
  ReloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { Alert, Badge, Button, Card, Col, Row, Space, Switch, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  type CallAuctionDashboard,
  type CallAuctionPick,
  fetchCallAuctionDashboard,
  runCallAuction,
} from './api';

// 集合竞价 LLM 快速选股页面，仅展示候选，不触发真实或模拟交易。
export function CallAuctionSection() {
  const [dashboard, setDashboard] = useState<CallAuctionDashboard | null>(null);
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastRefreshAt, setLastRefreshAt] = useState<string | null>(null);
  const [autoError, setAutoError] = useState<string | null>(null);
  const [skippedCount, setSkippedCount] = useState(0);
  const requestInFlightRef = useRef(false);

  const intervalSeconds = dashboard?.autoIntervalSeconds || 10;
  const marketOpen = dashboard?.marketStatus?.marketOpen === true;
  const collectOpen = dashboard?.marketStatus?.collectOpen === true;
  const decisionOpen = dashboard?.marketStatus?.decisionOpen === true;
  const auctionActive = marketOpen || collectOpen || decisionOpen;
  const latestRun = dashboard?.latestRun || null;
  const quoteSummary = dashboard?.quoteSummary || null;
  const llmFallback = hasLlmFallback(latestRun?.summary);

  const loadDashboard = useCallback(async (silent = false) => {
    if (requestInFlightRef.current) {
      setSkippedCount((value) => value + 1);
      return;
    }
    requestInFlightRef.current = true;
    if (!silent) setLoading(true);
    try {
      setDashboard(await fetchCallAuctionDashboard());
      setLastRefreshAt(formatDateTime(new Date()));
      setAutoError(null);
    } catch (error) {
      setAutoError(error instanceof Error ? error.message : '集合竞价数据加载失败');
    } finally {
      requestInFlightRef.current = false;
      if (!silent) setLoading(false);
    }
  }, []);

  const runOnce = useCallback(async (silent = false) => {
    if (requestInFlightRef.current) {
      setSkippedCount((value) => value + 1);
      return;
    }
    requestInFlightRef.current = true;
    if (!silent) setLoading(true);
    try {
      const result = await runCallAuction(5, false);
      if (result.skipped) {
        setAutoError(result.skipReason || '本轮集合竞价选股已跳过');
      } else if (result.status === 'COLLECTING') {
        setAutoError(null);
      } else {
        setAutoError(null);
      }
      setDashboard(await fetchCallAuctionDashboard());
      setLastRefreshAt(formatDateTime(new Date()));
    } catch (error) {
      setAutoError(error instanceof Error ? error.message : '集合竞价选股执行失败');
    } finally {
      requestInFlightRef.current = false;
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(() => {
      if (auctionActive) {
        void runOnce(true);
      } else {
        void loadDashboard(true);
      }
    }, Math.max(1, intervalSeconds) * 1000);
    return () => window.clearInterval(timer);
  }, [auctionActive, autoRefresh, intervalSeconds, loadDashboard, runOnce]);

  const columns = useMemo<ColumnsType<CallAuctionPick>>(
    () => [
      { title: '排名', dataIndex: 'rank', width: 64, fixed: 'left' },
      {
        title: '股票',
        dataIndex: 'symbol',
        width: 150,
        fixed: 'left',
        render: (_, row) => (
          <Space direction="vertical" size={0}>
            <Typography.Text strong>{row.symbol}</Typography.Text>
            <Typography.Text className="muted">{row.name}</Typography.Text>
          </Space>
        ),
      },
      { title: '参考价', dataIndex: 'latestPrice', width: 90, render: price },
      {
        title: '涨跌幅',
        dataIndex: 'pctChange',
        width: 90,
        render: (value) => <span style={{ color: changeColor(value) }}>{percent(value)}</span>,
      },
      { title: '竞价额', dataIndex: 'amount', width: 100, render: amount },
      { title: '量比', dataIndex: 'volumeRatio', width: 80, render: score },
      { title: '换手', dataIndex: 'turnoverRate', width: 80, render: percent },
      { title: '量化分', dataIndex: 'quantScore', width: 86, render: score },
      { title: 'LLM分', dataIndex: 'llmScore', width: 86, render: score },
      { title: '综合分', dataIndex: 'finalScore', width: 86, render: score },
      {
        title: '动作',
        dataIndex: 'action',
        width: 112,
        render: (value) => (
          <Tag color={value === 'BUY_CANDIDATE' ? 'red' : 'default'}>
            {value === 'BUY_CANDIDATE' ? '候选' : '观察'}
          </Tag>
        ),
      },
      { title: '理由', dataIndex: 'reason', width: 280 },
      { title: '风险', dataIndex: 'risk', width: 260 },
    ],
    [],
  );

  return (
    <Space direction="vertical" size={16} className="call-auction-workbench">
      <section className="t1-command-bar call-auction-command-bar">
        <Space direction="vertical" size={12} className="page-stack">
          <Space wrap>
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              loading={loading}
              disabled={!auctionActive}
              onClick={() => void runOnce()}
            >
              {decisionOpen ? '运行复核' : '采集快照'}
            </Button>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadDashboard()}>
              刷新
            </Button>
            <Space size={6}>
              <Typography.Text>自动刷新</Typography.Text>
              <Switch checked={autoRefresh} onChange={setAutoRefresh} />
            </Space>
            <Badge
              status={loading ? 'processing' : autoRefresh ? 'success' : 'default'}
              text={
                <span>
                  <ClockCircleOutlined /> {autoRefresh ? `${intervalSeconds}秒刷新` : '自动刷新未开启'}
                </span>
              }
            />
            <Tag color={decisionOpen ? 'red' : collectOpen ? 'green' : 'default'}>
              {decisionOpen ? '决策中' : collectOpen ? '采集中' : '非竞价窗口'}
            </Tag>
            <Tag color={llmFallback ? 'orange' : latestRun?.llmSuccess ? 'blue' : 'default'}>
              <RobotOutlined /> LLM {llmFallback ? '规则降级' : latestRun?.llmSuccess ? '已通过' : '未通过'}
            </Tag>
          </Space>
          <Space wrap>
            <Typography.Text className="muted">
              采集 {dashboard?.marketStatus?.collectWindow || '09:15-09:20'}，决策{' '}
              {dashboard?.marketStatus?.decisionWindow || '09:20-09:25'}，状态{' '}
              {dashboard?.marketStatus?.reason || '-'}
            </Typography.Text>
            <Typography.Text className="muted">
              最近运行 {latestRun?.snapshotTime || '-'}，最近刷新 {lastRefreshAt || '-'}
            </Typography.Text>
            <Typography.Text className="muted">
              行情 {latestRun?.quoteCount || 0}，规则候选 {latestRun?.candidateCount || 0}，
              LLM候选 {latestRun?.pickCount || 0}，融合 {quoteSummary?.quoteCount || 0}，
              跳过 {skippedCount} 次
            </Typography.Text>
          </Space>
          {!auctionActive && (
            <Alert showIcon type="info" message={dashboard?.marketStatus?.reason || '非集合竞价窗口'} />
          )}
          {latestRun?.status === 'COLLECTING' && (
            <Alert showIcon type="info" message="集合竞价采集期正在融合行情快照，09:20 后进入候选复核。" />
          )}
          {latestRun?.status === 'SKIPPED' && latestRun.skipReason && (
            <Alert showIcon type="warning" message={latestRun.skipReason} />
          )}
          {llmFallback && (
            <Alert showIcon type="warning" message="LLM 复核失败，本轮候选按量化规则降级输出。" />
          )}
          {autoError && <Alert showIcon type="warning" message={autoError} />}
        </Space>
      </section>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <MetricCard title="规则预筛" value={latestRun?.candidateCount || 0} />
        </Col>
        <Col xs={24} md={8}>
          <MetricCard title="LLM通过" value={latestRun?.pickCount || 0} />
        </Col>
        <Col xs={24} md={8}>
          <MetricCard title="融合快照" value={quoteSummary?.quoteCount || 0} />
        </Col>
      </Row>

      <Card className="t1-data-card">
        <Table<CallAuctionPick>
          rowKey="id"
          columns={columns}
          dataSource={dashboard?.picks || []}
          loading={loading}
          pagination={{ pageSize: 10 }}
          scroll={{ x: 1560 }}
        />
      </Card>
    </Space>
  );
}

// 指标卡片保持和 T+1 工作台一致的紧凑视觉。
function MetricCard({ title, value }: { title: string; value: number }) {
  return (
    <Card className="t1-metric-card">
      <Typography.Text className="muted">{title}</Typography.Text>
      <div className="call-auction-metric">{value}</div>
    </Card>
  );
}

// 从运行摘要中读取 LLM 降级标记，兼容后端新旧摘要结构。
function hasLlmFallback(summary?: Record<string, unknown>) {
  if (!summary) return false;
  if (summary.llmFallback === true) return true;
  const llm = summary.llm;
  return typeof llm === 'object' && llm !== null && 'llmFallback' in llm
    ? (llm as { llmFallback?: boolean }).llmFallback === true
    : false;
}

// 价格格式化，空值显示横线。
function price(value?: number | null) {
  const numeric = Number(value || 0);
  return numeric > 0 ? numeric.toFixed(2) : '-';
}

// 百分比格式化，空值显示横线。
function percent(value?: number | null) {
  const numeric = Number(value || 0);
  return `${numeric.toFixed(2)}%`;
}

// 分数格式化，空值显示横线。
function score(value?: number | null) {
  if (value === null || value === undefined) return '-';
  return Number(value).toFixed(2);
}

// 金额格式化为万/亿。
function amount(value?: number | null) {
  const numeric = Number(value || 0);
  if (numeric >= 100000000) return `${(numeric / 100000000).toFixed(2)}亿`;
  if (numeric >= 10000) return `${(numeric / 10000).toFixed(2)}万`;
  return numeric > 0 ? numeric.toFixed(0) : '-';
}

// A股习惯上涨红色、下跌绿色。
function changeColor(value?: number | null) {
  const numeric = Number(value || 0);
  if (numeric > 0) return '#d92d20';
  if (numeric < 0) return '#039855';
  return '#667085';
}

// 格式化本地刷新时间。
function formatDateTime(value: Date) {
  return value.toLocaleTimeString('zh-CN', { hour12: false });
}
