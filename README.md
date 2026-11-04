# ICC 本地工作台（icc-app）

一个本地运行的 A 股虚拟盘工作台：行情增量同步、自选股管理、多账户模拟交易、
时间指针回放、净值曲线，全部是服务端渲染的网页，**不依赖前端 JavaScript**。

不接实盘、不需要券商账号，只做个人研究和模拟。

## 功能

- **多账户**：每个账户独立现金、持仓、成交、净值，页面顶部切换或新建。
- **时间指针（asof）**：每个账户有自己的「虚拟当前日期」。
  - asof = 今天 → 实时模式，用盘中快照价。
  - asof < 今天 → 回放模式，只用该日及之前的日线收盘价，成交也记在 asof 当天。
  - 支持「前进一天 / 回到今天 / 跳到指定日期」。
- **K 线**：只画到时间指针为止，SVG 蜡烛图，服务端生成。
- **自选股**：增删、分组；添加时自动拉取名称与近 3 年日线。
- **行情同步**：范围 = 自选 ∪ 持仓 ∪ 沪深300；日线按库内最后日期增量拉取，快照全量刷新。
- **虚拟盘交易**：整手 100 股、佣金 / 印花税 / 过户费 / 滑点，资金不足自动缩量。
- **净值**：按收盘落快照，与沪深300 归一化对比，给出超额收益。
- **命令行**：`cli.py` 可直接查询与调仓，输出 `--json` 便于 AI 或脚本调用。
- **定时任务**：工作日 15:35 收盘同步；盘中每 5 分钟刷新快照。

## 快速开始

需要 Python 3.10+。

```bash
git clone <repo-url> icc-app
cd icc-app
python -m pip install -r requirements.txt

python app.py --sync   # 首次同步行情（会拉约 3 年日线）
python app.py          # 打开 http://127.0.0.1:8770
```

常用参数：

```bash
python app.py --port 8771
python app.py --host 0.0.0.0
python app.py --no-scheduler
python app.py --version
```

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 总览：指数卡片、账户净值、持仓与自选摘要 |
| `/watchlist` | 自选股增删与分组 |
| `/quotes` | 行情同步、快照与涨跌、日线入库情况 |
| `/positions` | 持仓明细、费用模型、账户重置 |
| `/trade` | 下单（买/卖）、最近成交 |
| `/trades` | 成交流水 |
| `/equity` | 净值曲线、沪深300 对比 |
| `/kline?symbol=600519.SH&n=60` | 截止时间指针的 K 线 |

## 命令行

```bash
python cli.py accounts                 # 列出账户
python cli.py status                   # 现金 / 市值 / 净值 / 持仓
python cli.py buy 600519.SH 100        # 价格留空 = 当前时间点可用价
python cli.py sell 600519.SH 40 --note 止盈
python cli.py asof --next              # 时间指针前进一个交易日
python cli.py asof --set 2026-10-09
python cli.py asof --today
python cli.py quote 600519.SH
python cli.py kline 600519.SH --n 10
python cli.py list                     # 自选股在当前时间点的价格
python cli.py sync
python cli.py --account 回放账户 status  # 指定账户（id 或名称）
python cli.py --json status
```

## 费用模型

与 `invest-council` 技能里的 PaperBroker 保持一致：

| 项目 | 值 |
|---|---|
| 佣金 | 0.025%，最低 5 元 |
| 印花税 | 卖出 0.1% |
| 过户费 | 买卖双向 0.001% |
| 滑点 | 5 bp |
| 最小交易单位 | 100 股 |

买入价 = 行情价 ×(1+滑点)，卖出价 = 行情价 ×(1−滑点)，费用从现金扣除；
卖出记录本次实现盈亏。回放模式下成交价取 asof 当日收盘价（无盘中价）。

## 数据与隐私

- 所有运行数据在本地 SQLite：默认 `data/app.db`，可用环境变量覆盖：

  ```bash
  ICC_APP_DATA=/path/to/my-data python app.py
  ```

- `data/`、`*.db`、`.env` 等已在 `.gitignore` 中排除，**账户、持仓、成交、净值不会被提交**。
- 仓库里只有代码与模板，不含任何行情数据、账号或凭据。
- 应用只在本机 `127.0.0.1` 监听；改成 `--host 0.0.0.0` 前请自行考虑局域网暴露风险。
- 数据库结构与 `invest-council` 技能的工作空间（`~/.icc/workspace`）完全隔离。

## 目录结构

```
icc-app/
├── app.py            # FastAPI 路由、页面、定时任务
├── cli.py            # 命令行调仓与查询
├── db.py             # SQLite 建表、账户、时间指针
├── broker.py         # 费用模型、成交、组合估值
├── feed.py           # 东财 push2/push2his，腾讯兜底
├── sync.py           # 日线增量 + 快照 + 净值落库
├── templates/        # Jinja2 页面
├── static/style.css
├── requirements.txt
└── data/             # 运行期生成，已忽略
```

## 数据来源与免责声明

- 行情来自东方财富公开接口（失败时回退腾讯行情），仅用于个人研究与学习。
- 本项目**不含实盘交易能力**，不提供任何投资建议，所有评分、净值、盈亏均为模拟结果。
- 数据可能存在延迟、缺失或错误，据此做出的任何决策与作者无关。
- 使用前请确认你的使用方式符合数据提供方的条款与当地法规。

## License

MIT，见 [LICENSE](LICENSE)。
