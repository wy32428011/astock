import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const args = new Map();
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1]);
}

const baseUrl = args.get('--base-url') || 'http://127.0.0.1:5173';
const mode = args.get('--mode') || 'offline';
const outDir = path.resolve(args.get('--out-dir') || '../output/playwright');
const prefix = args.get('--prefix') || mode;
const expectedDate = args.get('--expected-date') || '';
const expectedPick = args.get('--expected-pick') || '';
const expectedPickSymbol = args.get('--expected-pick-symbol') || '';

await fs.mkdir(outDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const consoleErrors = [];

page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text());
});
page.on('pageerror', (error) => consoleErrors.push(error.message));

async function capture(name) {
  await page.waitForTimeout(1200);
  const screenshot = path.join(outDir, `${prefix}-${name}.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  return screenshot;
}

try {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });

  const screenshots = [];
  if (mode === 'online') {
    await page.getByText('股票总数', { exact: true }).waitFor({ timeout: 60000 });
    if (expectedDate) {
      await page.getByText(`最新交易日 ${expectedDate}`, { exact: false }).waitFor({ timeout: 60000 });
    }
    screenshots.push(await capture('overview'));
    let allBodyText = await page.locator('body').innerText();

    const canvasCounts = {};
    for (const [label, name] of [
      ['基础信息', 'stocks'],
      ['分段数据', 'segments'],
      ['历史数据', 'history'],
      ['分析数据', 'analysis'],
      ['3日分析', 'three-day-analysis'],
      ['实时交易', 'realtime'],
      ['T+1交易', 't1-trading'],
    ]) {
      await page.getByText(label, { exact: true }).click();
      if (name === 'segments' || name === 'history') {
        await page.locator('canvas').first().waitFor({ timeout: 60000 });
      }
      canvasCounts[name] = await page.locator('canvas').count();
      screenshots.push(await capture(name));
      allBodyText += `\n${await page.locator('body').innerText()}`;
    }

    const bodyText = allBodyText;
    const filteredErrors = consoleErrors.filter((item) => !item.includes('findDOMNode'));
    const result = {
      mode,
      hasTradeDate: expectedDate ? bodyText.includes(expectedDate) : true,
      hasPick: expectedPick ? bodyText.includes(expectedPick) : true,
      hasPickSymbol: expectedPickSymbol ? bodyText.includes(expectedPickSymbol) : true,
      hasUnavailableAlert: bodyText.includes('以下数据接口暂不可用'),
      hasStocksPage: bodyText.includes('A股基础信息') || bodyText.includes('代码'),
      hasSegmentsCanvas: canvasCounts.segments >= 4,
      hasHistoryCanvas: canvasCounts.history >= 2,
      hasAnalysisPage: bodyText.includes('分析日期'),
      hasThreeDayAnalysisPage: bodyText.includes('全A股未来3个交易日涨势候选'),
      hasRealtimePage: bodyText.includes('实时分析信号'),
      hasRealtimeAutoControls: bodyText.includes('自动刷新') && bodyText.includes('自动买卖'),
      hasRealtimePnlAmount: bodyText.includes('总盈亏') && bodyText.includes('持仓浮盈') && bodyText.includes('已实现盈亏'),
      hasRealtimeMarketStatus: bodyText.includes('市场状态') && (bodyText.includes('开盘中') || bodyText.includes('非开盘')),
      hasT1TradingPage:
        bodyText.includes('T+1 实时模拟工作台') &&
        bodyText.includes('初始总资产') &&
        bodyText.includes('设置总资产') &&
        bodyText.includes('14:05 T+1质量选股') &&
        bodyText.includes('LLM复核') &&
        bodyText.includes('14:05质量候选'),
      consoleErrors: filteredErrors,
      canvasCounts,
      screenshots,
    };
    if (
      !result.hasTradeDate ||
      (!result.hasPick && !result.hasPickSymbol) ||
      result.hasUnavailableAlert ||
      !result.hasSegmentsCanvas ||
      !result.hasHistoryCanvas ||
      !result.hasThreeDayAnalysisPage ||
      !result.hasRealtimePage ||
      !result.hasRealtimeAutoControls ||
      !result.hasRealtimePnlAmount ||
      !result.hasRealtimeMarketStatus ||
      !result.hasT1TradingPage ||
      result.consoleErrors.length > 0
    ) {
      console.log(JSON.stringify(result, null, 2));
      process.exitCode = 1;
    } else {
      console.log(JSON.stringify(result, null, 2));
    }
  } else {
    await page.getByText('以下数据接口暂不可用', { exact: false }).waitFor({ timeout: 100000 });
    screenshots.push(await capture('overview'));
    const overviewText = await page.locator('body').innerText();
    let allBodyText = overviewText;
    for (const [label, name] of [
      ['基础信息', 'stocks'],
      ['分段数据', 'segments'],
      ['历史数据', 'history'],
      ['分析数据', 'analysis'],
      ['3日分析', 'three-day-analysis'],
      ['实时交易', 'realtime'],
    ]) {
      await page.getByText(label, { exact: true }).click();
      screenshots.push(await capture(name));
      allBodyText += `\n${await page.locator('body').innerText()}`;
    }
    const bodyText = allBodyText;
    const filteredErrors = consoleErrors.filter(
      (item) => !item.includes('503 (Service Unavailable)') && !item.includes('findDOMNode'),
    );
    const result = {
      mode,
      hasUnavailableAlert: overviewText.includes('以下数据接口暂不可用'),
      hasDashboardTitle: overviewText.includes('A股数据工作台') || bodyText.includes('A股数据工作台'),
      hasStockTotalLabel: overviewText.includes('股票总数'),
      hasFalseZeroStockTotal: overviewText.includes('股票总数\n0'),
      hasAllMenus: ['数据概览', '基础信息', '分段数据', '历史数据', '分析数据', '3日分析', '实时交易'].every((text) =>
        bodyText.includes(text),
      ),
      hasRealtimeAutoControls: bodyText.includes('自动刷新') && bodyText.includes('自动买卖'),
      hasRealtimePnlAmount: bodyText.includes('总盈亏') && bodyText.includes('持仓浮盈') && bodyText.includes('已实现盈亏'),
      hasRealtimeMarketStatus: bodyText.includes('市场状态') && (bodyText.includes('开盘中') || bodyText.includes('非开盘')),
      consoleErrors: filteredErrors,
      screenshots,
    };
    if (
      !result.hasUnavailableAlert ||
      result.hasFalseZeroStockTotal ||
      !result.hasAllMenus ||
      !result.hasRealtimeAutoControls ||
      !result.hasRealtimePnlAmount ||
      !result.hasRealtimeMarketStatus ||
      result.consoleErrors.length > 0
    ) {
      console.log(JSON.stringify(result, null, 2));
      process.exitCode = 1;
    } else {
      console.log(JSON.stringify(result, null, 2));
    }
  }
} finally {
  await browser.close();
}
