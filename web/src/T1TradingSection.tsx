import {
  FundProjectionScreenOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RetweetOutlined,
  StopOutlined,
  ThunderboltOutlined,
  WalletOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  InputNumber,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useMemo, useState } from 'react';

const { Text } = Typography;

const API_BASE = '';

interface T1Account {
  accountId: string;
  initialCash: number;
  cash: number;
  marketValue: number;
  totalAsset: number;
  realizedPnl: number;
  totalPnl: number;
  updatedAt?: string;
}

interface T1Position {
  symbol: string;
  name: string;
  quantity: number;
  availableQuantity: number;
  avgCost: number;
  lastPrice: number;
  marketValue: number;
  floatingPnl: number;
  buyTradeDate?: string;
  availableFromDate?: string;
  updatedAt?: string;
}

interface T1Order {
  id: number;
  symbol: string;
  name: string;
  side: 'BUY' | 'SELL' | string;
  price: number;
  quantity: number;
  amount: number;
  fee: number;
  status: string;
  reason?: string;
  analysisDate?: string;
  tradeDate?: string;
  createdAt?: string;
}

interface T1Pick {
  rank: number;
  symbol: string;
  name: string;
  quantScore?: number;
  llmScore?: number;
  finalScore?: number;
  expectedDirection?: string;
  reason?: string;
  risk?: string;
  price: number;
  pctChange?: number;
}

interface T1TaskStatus {
  taskId: string;
  status: string;
  running: boolean;
  intervalSeconds: number;
  finalLimit: number;
  executeTrades: boolean;
  runCount: number;
  lastRunAt?: string;
  nextRunAt?: string;
  lastMessage?: string;
}

interface T1Dashboard {
  account: T1Account;
  positions: T1Position[];
  orders: T1Order[];
  picks: T1Pick[];
  analysisDate?: string;
  tradeDate?: string;
  summary: {
    candidateCount: number;
    positionCount: number;
    unavailablePositionCount: number;
  };
  marketStatus?: {
    marketOpen?: boolean;
    session?: string;
    reason?: string;
  };
  task?: T1TaskStatus;
}

interface T1RunResult {
  analysisDate?: string;
  tradeDate?: string;
  candidateCount: number;
  buyCount: number;
  sellCount: number;
  orderCount: number;
  quoteCount?: number;
  realtime?: boolean;
  marketOpen?: boolean;
  marketSession?: string;
  skipped: boolean;
  skipReason?: string;
}

/** T+1 页面通用请求方法。 */
async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as T;
}

/** 金额格式化。 */
function money(value?: number): string {
  return `¥${(value ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** 分数字段格式化。 */
function score(value?: number): string {
  return value === undefined || value === null ? '-' : value.toFixed(2);
}

/** A 股盈亏颜色，红色表示盈利，绿色表示亏损。 */
function pnlColor(value?: number): string {
  return (value ?? 0) >= 0 ? '#cf1322' : '#389e0d';
}

/** T+1 专属模拟交易和实时任务工作台。 */
export function T1TradingSection() {
  const [dashboard, setDashboard] = useState<T1Dashboard | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [taskRunning, setTaskRunning] = useState(false);
  const [initialCashInput, setInitialCashInput] = useState<number | null>(null);
  const [taskIntervalSec, setTaskIntervalSec] = useState(60);
  const [messageApi, contextHolder] = message.useMessage();

  const loadDashboard = useCallback(async () => {
    setLoading(true);
    try {
      const data = await requestJson<T1Dashboard>('/api/t1-trading');
      setDashboard(data);
    } catch (error) {
      messageApi.error(`加载 T+1 页面失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setLoading(false);
    }
  }, [messageApi]);

  const runT1Trading = useCallback(async (executeTrades: boolean) => {
    setRunning(true);
    try {
      const result = await requestJson<T1RunResult>('/api/t1-trading/run', {
        method: 'POST',
        body: JSON.stringify({ executeTrades, finalLimit: 20 }),
      });
      if (result.skipped) {
        messageApi.warning(result.skipReason || 'T+1 模拟未执行');
      } else {
        messageApi.success(
          executeTrades
            ? `T+1 模拟完成：买入 ${result.buyCount}，卖出 ${result.sellCount}`
            : `T+1 预演完成：候选 ${result.candidateCount}`,
        );
      }
      await loadDashboard();
    } catch (error) {
      messageApi.error(`执行 T+1 模拟失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setRunning(false);
    }
  }, [loadDashboard, messageApi]);

  const runT1RealtimeTrading = useCallback(async () => {
    setRunning(true);
    try {
      const result = await requestJson<T1RunResult>('/api/t1-trading/realtime-run', {
        method: 'POST',
        body: JSON.stringify({ executeTrades: true, finalLimit: 20 }),
      });
      if (result.skipped) {
        messageApi.warning(result.skipReason || '当前未执行 T+1 实时模拟');
      } else {
        messageApi.success(`实时模拟完成：行情 ${result.quoteCount ?? 0}，买入 ${result.buyCount}，卖出 ${result.sellCount}`);
      }
      await loadDashboard();
    } catch (error) {
      messageApi.error(`实时执行失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setRunning(false);
    }
  }, [loadDashboard, messageApi]);

  const runT1Analysis = useCallback(async () => {
    setRunning(true);
    try {
      await requestJson('/api/t1-analysis/run', {
        method: 'POST',
        body: JSON.stringify({ preselectLimit: 120, finalLimit: 20 }),
      });
      messageApi.success('T+1 分析已完成');
      await loadDashboard();
    } catch (error) {
      messageApi.error(`运行 T+1 分析失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setRunning(false);
    }
  }, [loadDashboard, messageApi]);

  const resetT1Account = useCallback(async () => {
    const initialCash = initialCashInput ?? dashboard?.account.initialCash ?? 1000000;
    if (initialCash <= 0) {
      messageApi.warning('请输入大于 0 的总资产');
      return;
    }
    setRunning(true);
    try {
      await requestJson('/api/t1-trading/reset', { method: 'POST', body: JSON.stringify({ initialCash }) });
      messageApi.success(`T+1 模拟账户已设置为 ${money(initialCash)}`);
      await loadDashboard();
    } catch (error) {
      messageApi.error(`重置 T+1 账户失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setRunning(false);
    }
  }, [dashboard?.account.initialCash, initialCashInput, loadDashboard, messageApi]);

  const startT1Task = useCallback(async () => {
    setTaskRunning(true);
    try {
      await requestJson<T1TaskStatus>('/api/t1-trading/task/start', {
        method: 'POST',
        body: JSON.stringify({ intervalSeconds: taskIntervalSec, finalLimit: 20, executeTrades: true }),
      });
      messageApi.success('T+1 到期执行任务已启动');
      await loadDashboard();
    } catch (error) {
      messageApi.error(`启动任务失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setTaskRunning(false);
    }
  }, [loadDashboard, messageApi, taskIntervalSec]);

  const stopT1Task = useCallback(async () => {
    setTaskRunning(true);
    try {
      await requestJson<T1TaskStatus>('/api/t1-trading/task/stop', { method: 'POST' });
      messageApi.success('T+1 到期执行任务已停止');
      await loadDashboard();
    } catch (error) {
      messageApi.error(`停止任务失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setTaskRunning(false);
    }
  }, [loadDashboard, messageApi]);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  useEffect(() => {
    if (dashboard?.account.initialCash && initialCashInput === null) {
      setInitialCashInput(dashboard.account.initialCash);
    }
  }, [dashboard?.account.initialCash, initialCashInput]);

  useEffect(() => {
    if (dashboard?.task?.intervalSeconds) {
      setTaskIntervalSec(dashboard.task.intervalSeconds);
    }
  }, [dashboard?.task?.intervalSeconds]);

  const positionColumns = useMemo<ColumnsType<T1Position>>(() => [
    { title: '代码', dataIndex: 'symbol', width: 110, fixed: 'left' },
    { title: '名称', dataIndex: 'name', width: 120 },
    { title: '数量', dataIndex: 'quantity', align: 'right' },
    {
      title: '可卖数量',
      dataIndex: 'availableQuantity',
      align: 'right',
      render: (value: number) => value > 0 ? <Tag color="green">{value}</Tag> : <Tag color="orange">T+1锁定</Tag>,
    },
    { title: '成本价', dataIndex: 'avgCost', align: 'right', render: (value: number) => value.toFixed(3) },
    { title: '最新价', dataIndex: 'lastPrice', align: 'right', render: (value: number) => value.toFixed(3) },
    { title: '市值', dataIndex: 'marketValue', align: 'right', render: money },
    {
      title: '浮动盈亏',
      dataIndex: 'floatingPnl',
      align: 'right',
      render: (value: number) => <Text style={{ color: pnlColor(value) }}>{money(value)}</Text>,
    },
    { title: '买入日', dataIndex: 'buyTradeDate', width: 120 },
    { title: '可卖日', dataIndex: 'availableFromDate', width: 120 },
  ], []);

  const pickColumns = useMemo<ColumnsType<T1Pick>>(() => [
    { title: '排名', dataIndex: 'rank', width: 80 },
    { title: '代码', dataIndex: 'symbol', width: 110 },
    { title: '名称', dataIndex: 'name', width: 120 },
    { title: '综合评分', dataIndex: 'finalScore', align: 'right', render: score },
    { title: '量化评分', dataIndex: 'quantScore', align: 'right', render: score },
    { title: 'LLM评分', dataIndex: 'llmScore', align: 'right', render: score },
    { title: '交易价', dataIndex: 'price', align: 'right', render: (value: number) => value.toFixed(3) },
    { title: '预期', dataIndex: 'expectedDirection', width: 120 },
    { title: '理由', dataIndex: 'reason', ellipsis: true },
    { title: '风险', dataIndex: 'risk', ellipsis: true },
  ], []);

  const orderColumns = useMemo<ColumnsType<T1Order>>(() => [
    { title: '时间', dataIndex: 'createdAt', width: 170 },
    { title: '代码', dataIndex: 'symbol', width: 110 },
    { title: '名称', dataIndex: 'name', width: 120 },
    {
      title: '方向',
      dataIndex: 'side',
      width: 90,
      render: (value: string) => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value === 'BUY' ? '买入' : '卖出'}</Tag>,
    },
    { title: '价格', dataIndex: 'price', align: 'right', render: (value: number) => value.toFixed(3) },
    { title: '数量', dataIndex: 'quantity', align: 'right' },
    { title: '金额', dataIndex: 'amount', align: 'right', render: money },
    { title: '手续费', dataIndex: 'fee', align: 'right', render: money },
    { title: '分析日', dataIndex: 'analysisDate', width: 120 },
    { title: '交易日', dataIndex: 'tradeDate', width: 120 },
    { title: '原因', dataIndex: 'reason', ellipsis: true },
  ], []);

  const account = dashboard?.account;
  const task = dashboard?.task;
  const marketOpen = dashboard?.marketStatus?.marketOpen === true;
  const taskStatusColor = task?.running ? 'processing' : 'default';

  return (
    <Space direction="vertical" size={16} className="t1-workbench">
      {contextHolder}

      <section className="t1-command-bar">
        <div>
          <Space size={8} wrap>
            <Text strong>T+1 实时模拟工作台</Text>
            <Tag color="blue">模拟交易</Tag>
            <Tag color={marketOpen ? 'red' : 'default'}>{marketOpen ? '开盘中' : '非开盘'}</Tag>
            <Badge status={taskStatusColor} text={task?.running ? '任务运行中' : '任务已停止'} />
          </Space>
          <div className="t1-subline">
            分析日 {dashboard?.analysisDate || '-'} · T+1交易日 {dashboard?.tradeDate || '-'} · 下次到期 {task?.nextRunAt || '-'}
          </div>
        </div>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={loadDashboard}>刷新</Button>
      </section>

      <Row gutter={[12, 12]}>
        <Col xs={24} sm={12} lg={6}>
          <Card size="small" className="t1-metric-card">
            <Statistic title="总资产" value={account?.totalAsset ?? 0} formatter={(value) => money(Number(value))} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card size="small" className="t1-metric-card">
            <Statistic
              title="总盈亏"
              value={account?.totalPnl ?? 0}
              valueStyle={{ color: pnlColor(account?.totalPnl) }}
              formatter={(value) => money(Number(value))}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card size="small" className="t1-metric-card">
            <Statistic title="现金" value={account?.cash ?? 0} formatter={(value) => money(Number(value))} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card size="small" className="t1-metric-card">
            <Statistic
              title="可卖 / 持仓"
              value={(dashboard?.summary.positionCount ?? 0) - (dashboard?.summary.unavailablePositionCount ?? 0)}
              suffix={`/ ${dashboard?.summary.positionCount ?? 0}`}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={[12, 12]} align="stretch">
        <Col xs={24} xl={15}>
          <Card title="交易操作" size="small" className="t1-control-card">
            <Space direction="vertical" size={14} style={{ width: '100%' }}>
              <Space wrap>
                <Button icon={<FundProjectionScreenOutlined />} loading={running} onClick={runT1Analysis}>
                  运行T+1分析
                </Button>
                <Button icon={<PlayCircleOutlined />} loading={running} onClick={() => runT1Trading(false)}>
                  预演
                </Button>
                <Button icon={<RetweetOutlined />} loading={running} onClick={() => runT1Trading(true)}>
                  执行T+1模拟
                </Button>
                <Button type="primary" icon={<ThunderboltOutlined />} loading={running} onClick={runT1RealtimeTrading}>
                  实时执行一次
                </Button>
              </Space>
              <Space wrap>
                <InputNumber
                  min={1}
                  step={10000}
                  precision={2}
                  value={initialCashInput ?? account?.initialCash ?? 1000000}
                  onChange={(value) => setInitialCashInput(typeof value === 'number' ? value : Number(value || 0))}
                  addonBefore="初始总资产"
                  style={{ width: 240 }}
                />
                <Button danger icon={<WalletOutlined />} loading={running} onClick={resetT1Account}>
                  设置总资产
                </Button>
                <Text type="secondary">设置总资产会重置 T+1 专属账户、持仓和订单。</Text>
              </Space>
            </Space>
          </Card>
        </Col>
        <Col xs={24} xl={9}>
          <Card title="到期执行任务" size="small" className="t1-control-card">
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Space wrap>
                <Tag color={task?.running ? 'green' : 'default'}>{task?.running ? '运行中' : '已停止'}</Tag>
                <Select
                  value={taskIntervalSec}
                  style={{ width: 120 }}
                  onChange={setTaskIntervalSec}
                  options={[15, 30, 60, 120].map((value) => ({ value, label: `${value} 秒` }))}
                />
                <Button type="primary" icon={<PlayCircleOutlined />} loading={taskRunning} onClick={startT1Task}>
                  启动任务
                </Button>
                <Button danger icon={<StopOutlined />} loading={taskRunning} onClick={stopT1Task}>
                  停止任务
                </Button>
              </Space>
              <div className="t1-task-grid">
                <span>执行次数</span><strong>{task?.runCount ?? 0}</strong>
                <span>上次执行</span><strong>{task?.lastRunAt || '-'}</strong>
                <span>最近状态</span><strong>{task?.lastMessage || '-'}</strong>
              </div>
            </Space>
          </Card>
        </Col>
      </Row>

      <Alert
        type="info"
        showIcon
        message="T+1 专属规则"
        description="实时执行会读取真实实时行情价格撮合模拟单；买入当日不可卖，下一交易日起释放可卖数量。所有交易均为模拟，不连接真实券商。"
      />

      <Card size="small" className="t1-data-card">
        <Tabs
          items={[
            {
              key: 'positions',
              label: `持仓 ${dashboard?.summary.positionCount ?? 0}`,
              children: (
                <Table
                  rowKey="symbol"
                  loading={loading}
                  dataSource={dashboard?.positions ?? []}
                  columns={positionColumns}
                  size="small"
                  scroll={{ x: 1120 }}
                  pagination={{ pageSize: 10 }}
                />
              ),
            },
            {
              key: 'picks',
              label: `候选 ${dashboard?.summary.candidateCount ?? 0}`,
              children: (
                <Table
                  rowKey="symbol"
                  loading={loading}
                  dataSource={dashboard?.picks ?? []}
                  columns={pickColumns}
                  size="small"
                  scroll={{ x: 1260 }}
                  pagination={{ pageSize: 10 }}
                />
              ),
            },
            {
              key: 'orders',
              label: `订单 ${dashboard?.orders.length ?? 0}`,
              children: (
                <Table
                  rowKey="id"
                  loading={loading}
                  dataSource={dashboard?.orders ?? []}
                  columns={orderColumns}
                  size="small"
                  scroll={{ x: 1280 }}
                  pagination={{ pageSize: 10 }}
                />
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
