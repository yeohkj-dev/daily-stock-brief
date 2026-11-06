# 观察清单每日简报

网页：https://yeohkj-dev.github.io/daily-stock-brief/ （历史报告：`archive/`）

GitHub Actions 每个交易日自动跑两次，不依赖任何 Claude 账号，也不用付费：

| 版本 | 美东时间 | 马来西亚时间（冬令时 / 夏令时） | 主要看 |
|---|---|---|---|
| 盘前版 | 约 07:15–09:15 | 约 20:15–22:15 / 19:15–21:15 | 盘前涨跌、昨日表现、隔夜新闻 |
| 收盘版 | 约 16:45–17:45 | 次日约 05:45–06:45 / 04:45–05:45 | 今日涨跌、盘后涨跌、量比、当天新闻 |

GitHub 的排程常常晚 10–60 分钟。周末和美股休市日会自动跳过。

## 每份报告的内容
1. **每日动向**：价格、盘前 / 今日 / 盘后涨跌、5 日、1 月、年初至今、量比
2. **阶段与趋势**：Stage 1–4（按 50 / 200 天线和 200 天线方向判断，均线与 TradingView 核对），中期 / 长期趋势
3. **为何大涨大跌**：涨跌超过门槛的股票，列出最相关的报道、原因类别（财报、指引、评级、订单、并购、融资、监管、板块……），以及和 QQQ / SMH 的对比（跟随大盘还是个股因素）
4. **新闻摘要**：每只股票最近 24 小时的新闻（Yahoo Finance + Google News），中文标题和摘要附英文原文，每条都有发布日期时间

## Telegram 推送
收盘版生成后（马来西亚早上），自动发一份简报到 Telegram：大盘、数据核对结果、每只大涨大跌股票的阶段和主要原因（附新闻时间）、各阶段名单、完整报告链接。
- 需要在 repo 的 **Settings → Secrets and variables → Actions** 加两个 secret：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（可以和 jlaw-daily-report 用同一个 bot）。
- 想改推哪一版：`config.yml` 的 `telegram:`（close / premarket / both / off）。
- 同一份只发一次；没设 secret 时只是跳过，不影响网站。

## 怎么改
- **改股票清单**：编辑 `watchlist.txt`（一行一个代号），按 Commit。
- **改门槛 / 新闻数量**：编辑 `config.yml`。
- **马上跑一次**：Actions → 观察清单简报 → Run workflow（可选盘前版或收盘版）。
- **休市日**：`tools/stock_brief.py` 里的 `HOLIDAYS`，每年底补下一年。

## 在自己电脑上跑
```
python tools/stock_brief.py --force
```
输出 `docs/index.html`。只用 Python 标准库，不用安装任何东西。

数据来自 TradingView（价格可能延迟约 15 分钟）、Yahoo Finance 和 Google News 的 RSS。中文是机器翻译；"原因"按新闻关键词和大盘对比自动归类，是推断，不保证正确。

**仅供学习，不构成投资建议。**
