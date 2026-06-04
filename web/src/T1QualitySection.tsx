import {
  ClockCircleOutlined,
  FundProjectionScreenOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Col,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useMemo, useState } from 'react';

const { Paragraph, Text } = Typography;

const API_BASE = '';

interface T1QualityRun {
  id?: number;
  runId?: number;
  tradeDate?: string;
  snapshotTime?: string;
  triggerType?: string;
  status?: string;
  quoteCount?: number;
  validQuoteCount?: number;
  candidateCount?: number;
  pickCount?: number;
  buyCount?: number;
  sellCount?: number;
  executeTrades?: boolean;
  llmRequired?: boolean;
  llmSuccess?: boolean;
  marketSession?: string;
  skipped?: boolean;
  skipReason?: string;
  errorMessage?: string;
  createdAt?: string;
}

interface T1QualityPick {
  id: number;
  runId: number;
  rank: number;
  tradeDate?: string;
  snapshotTime?: string;
  symbol: string;
  name: string;
  latestPrice: number;
  pctChange: number;
  volumeRatio: number;
  turnoverRate: number;
  trendScore: number;
  momentumScore: number;
  liquidityScore: number;
  riskScore: number;
  quantScore: number;
  llmScore?: number | null;
  finalScore: number;
  action?: string;
  expectedDirection?: string;
  reason?: string;
  risk?: string;
}

interface T1QualityDashboard {
  latestRun?: T1QualityRun | null;
  picks: T1QualityPick[];
}

// 质量选股页面通用请求方法，保持和 T+1 工作台相同的后端协议。
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

// 分数格式化，所有质量因子统一保留两位。
function score(value?: number | null): string {
  return value === undefined || value === null ? '-' : Number(value).toFixed(2);
}

// 价格格式化，避免表格里出现 undefined 或过长小数。
function price(value?: number | null): string {
  return value === undefined || value === null ? '-' : Number(value).toFixed(2);
}

// 百分比格式化，用于涨跌幅、量比和换手率展示。
function percent(value?: number | null): string {
  return value === undefined || value === null ? '-' : `${Number(value).toFixed(2)}%`;
}

// 根据运行状态生成 Ant Design 标签颜色。
function statusColor(status?: string): string {
  if (status === 'SUCCESS') return 'green';
  if (status === 'SKIPPED') return 'orange';
  if (status === 'FAILED') return 'red';
  return 'default';
}

// 14:05 T+1 盘中质量选股专属页面。
export function T1QualitySection() {
  const [dashboard, setDashboard] = useState<T1QualityDashboard | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [messageApi, contextHolder] = message.useMessage();

  const latestRun = dashboard?.latestRun ?? null;

  const loadDashboard = useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    try {
      setDashboard(await requestJson<T1QualityDashboard>('/api/t1-quality?finalLimit=50'));
    } catch (error) {
      messageApi.error(`加载14:05质量选股页面失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [messageApi]);

  const runQuality = useCallback(async (force = false) => {
    setRunning(true);
    try {
      const result = await requestJson<T1QualityRun>('/api/t1-quality/run', {
        method: 'POST',
        body: JSON.stringify({ executeTrades: true, finalLimit: 20, force }),
      });
      if (result.skipped || result.status === 'SKIPPED') {
        messageApi.warning(result.skipReason || '14:05 T+1质量选股已跳过');
      } else {
        messageApi.success(`14:05质量选股完成：候选 ${result.pickCount ?? 0}，买入 ${result.buyCount ?? 0}`);
      }
      await loadDashboard();
    } catch (error) {
      messageApi.error(`运行14:05质量选股失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setRunning(false);
    }
  }, [loadDashboard, messageApi]);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  const columns = useMemo<ColumnsType<T1QualityPick>>(() => [
    { title: '排名', dataIndex: 'rank', width: 72, fixed: 'left' },
    { title: '代码', dataIndex: 'symbol', width: 110, fixed: 'left' },
    { title: '名称', dataIndex: 'name', width: 120 },
    { title: '最新价', dataIndex: 'latestPrice', align: 'right', width: 100, render: price },
    {
      title: '涨跌幅',
      dataIndex: 'pctChange',
      align: 'right',
      width: 100,
      render: (value?: number | null) => <Text type={(value ?? 0) >= 0 ? 'danger' : 'success'}>{percent(value)}</Text>,
    },
    { title: '综合分', dataIndex: 'finalScore', align: 'right', width: 100, render: score },
    { title: '量化分', dataIndex: 'quantScore', align: 'right', width: 100, render: score },
    { title: 'LLM分', dataIndex: 'llmScore', align: 'right', width: 100, render: score },
    { title: '趋势', dataIndex: 'trendScore', align: 'right', width: 90, render: score },
    { title: '动量', dataIndex: 'momentumScore', align: 'right', width: 90, render: score },
    { title: '流动性', dataIndex: 'liquidityScore', align: 'right', width: 90, render: score },
    { title: '风险', dataIndex: 'riskScore', align: 'right', width: 90, render: score },
    { title: '量比', dataIndex: 'volumeRatio', align: 'right', width: 90, render: score },
    { title: '换手率', dataIndex: 'turnoverRate', align: 'right', width: 100, render: percent },
    {
      title: '动作',
      dataIndex: 'action',
      width: 90,
      render: (value?: string) => <Tag color={value === 'BUY' ? 'red' : 'default'}>{value || 'WATCH'}</Tag>,
    },
    { title: '预期', dataIndex: 'expectedDirection', width: 150 },
    {
      title: '理由',
      dataIndex: 'reason',
      width: 320,
      render: (value?: string) => <Paragraph ellipsis={{ rows: 2 }} className="quality-table-text">{value || '-'}</Paragraph>,
    },
    {
      title: '风险',
      dataIndex: 'risk',
      width: 300,
      render: (value?: string) => <Paragraph ellipsis={{ rows: 2 }} className="quality-table-text">{value || '-'}</Paragraph>,
    },
  ], []);

  return (
    <>
      {contextHolder}
      <Space direction="vertical" size={16} className="t1-workbench quality-workbench">
        <section className="t1-command-bar quality-command-bar">
          <div>
            <Space size={8} wrap>
              <ClockCircleOutlined />
              <Text strong>14:05 T+1 盘中质量选股</Text>
              <Tag color={statusColor(latestRun?.status)}>{latestRun?.status || '暂无运行'}</Tag>
              <Tag color={latestRun?.llmSuccess ? 'purple' : 'default'}>
                LLM复核{latestRun?.llmSuccess ? '成功' : '等待/失败'}
              </Tag>
            </Space>
            <div className="t1-subline">
              交易日 {latestRun?.tradeDate || '-'} · 快照 {latestRun?.snapshotTime || '-'} · 触发 {latestRun?.triggerType || '-'}
            </div>
          </div>
          <Space wrap>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadDashboard()}>
              刷新
            </Button>
            <Button type="primary" icon={<PlayCircleOutlined />} loading={running} onClick={() => void runQuality(false)}>
              运行质量选股
            </Button>
            <Button icon={<FundProjectionScreenOutlined />} loading={running} onClick={() => void runQuality(true)}>
              强制补跑
            </Button>
          </Space>
        </section>

        <Row gutter={[12, 12]}>
          <Col xs={24} sm={12} xl={6}>
            <Card size="small" className="t1-metric-card">
              <Statistic title="原始行情" value={latestRun?.quoteCount ?? 0} suffix="条" />
            </Card>
          </Col>
          <Col xs={24} sm={12} xl={6}>
            <Card size="small" className="t1-metric-card">
              <Statistic title="有效行情" value={latestRun?.validQuoteCount ?? 0} suffix="条" />
            </Card>
          </Col>
          <Col xs={24} sm={12} xl={6}>
            <Card size="small" className="t1-metric-card">
              <Statistic title="质量候选" value={latestRun?.pickCount ?? dashboard?.picks.length ?? 0} suffix="只" />
            </Card>
          </Col>
          <Col xs={24} sm={12} xl={6}>
            <Card size="small" className="t1-metric-card">
              <Statistic title="模拟买入" value={latestRun?.buyCount ?? 0} suffix="笔" />
            </Card>
          </Col>
        </Row>

        {(latestRun?.skipReason || latestRun?.errorMessage) && (
          <Alert
            showIcon
            type={latestRun?.status === 'FAILED' ? 'error' : 'warning'}
            message={latestRun.skipReason || latestRun.errorMessage}
          />
        )}

        <Card size="small" className="t1-control-card">
          <div className="t1-task-grid quality-run-grid">
            <span>运行编号</span><strong>{latestRun?.id ?? '-'}</strong>
            <span>市场时段</span><strong>{latestRun?.marketSession || '-'}</strong>
            <span>候选池</span><strong>{latestRun?.candidateCount ?? 0}</strong>
            <span>自动交易</span><strong>{latestRun?.executeTrades ? '启用' : '未启用'}</strong>
            <span>LLM要求</span><strong>{latestRun?.llmRequired ? '必须通过' : '非必需'}</strong>
            <span>创建时间</span><strong>{latestRun?.createdAt || '-'}</strong>
          </div>
        </Card>

        <Card size="small" className="t1-control-card quality-rule-card">
          <Space direction="vertical" size={10}>
            <Text strong>盘中质量选股规则</Text>
            <Paragraph>
              该页面只展示 14:05 T+1 盘中质量选股链路：实时行情生成质量候选，LLM 严格复核通过后才复用
              T+1 专属账户模拟买入。
            </Paragraph>
            <Paragraph>
              买入当日不可卖，下一交易日起释放可卖数量；LLM 超时、不可用或未保留候选时，运行记录会写入
              SKIPPED，系统不会自动买入。
            </Paragraph>
            <Paragraph>
              “运行质量选股”遵守默认时间窗和当天成功运行检查，“强制补跑”会跳过这些限制，适合人工调试和补录。
            </Paragraph>
          </Space>
        </Card>

        <Card size="small" className="t1-data-card quality-data-card">
          <Table
            rowKey="id"
            loading={loading}
            dataSource={dashboard?.picks ?? []}
            columns={columns}
            size="small"
            scroll={{ x: 2380 }}
            pagination={{ pageSize: 12 }}
          />
        </Card>
      </Space>
    </>
  );
}
