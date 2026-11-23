# 日志采集与事件追踪平台

面向应用日志收集、事件埋点追踪、流式采集与结构化归集的多源日志采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 日志源管理 | `log_source` | 日志源 | 源编号、源名称、日志类型 |
| 日志解析 | `log_parser` | 解析规则 | 规则编号、关联源、正则表达式 |
| 事件埋点 | `event_tracker` | 埋点事件 | 事件编号、事件名称、埋点页面 |
| 流式缓冲 | `stream_buffer` | 缓冲配置 | 配置编号、关联源、缓冲区大小 |
| 字段映射 | `field_mapping` | 映射规则 | 规则编号、源字段、目标字段 |
| 采集代理 | `agent_heartbeat` | 采集代理 | 代理编号、部署主机、代理版本 |
| 事件校验 | `event_validate` | 校验规则 | 规则编号、关联事件、字段检查 |
| 日志轮转 | `log_rotate` | 轮转策略 | 策略编号、关联源、轮转间隔 |
| 日志富化 | `enrich_join` | 富化规则 | 规则编号、目标字段、维表来源 |
| 告警规则 | `alert_rule` | 告警规则 | 规则编号、关联源、触发条件 |
| 日志分区 | `log_partition` | 分区规则 | 规则编号、关联源、分区键 |
| 采集位点 | `collect_checkpoint` | 位点记录 | 记录编号、关联源、文件偏移 |
| 窗口去重 | `dedup_window` | 去重窗口 | 窗口编号、关联源、去重键 |
| 聚合计算 | `aggregate_olap` | 聚合规则 | 规则编号、关联源、聚合维度 |
| 格式输出 | `format_output` | 输出配置 | 配置编号、关联源、输出格式 |
| 回溯采集 | `backfill_range` | 回溯任务 | 任务编号、关联源、时间范围 |
| 采样策略 | `sampling_rule` | 采样规则 | 规则编号、关联源、采样方式 |
| 质量诊断 | `log_quality` | 诊断报告 | 报告编号、关联源、解析成功率 |

## 质量诊断：历史对照与复算

`质量诊断` 工作台（模块 `log_quality`）在通用列表之外，提供历史诊断对照的完整链路：

- **报告留证**：每次诊断都会固化当时的解析规则快照、原始日志行号/偏移范围、内容摘要
  和证据快照文件（`data/quality_evidence/`）。之后更新解析配置，旧报告仍能定位到原日志，
  原数字也不会被改写。
- **按版本查询**：`GET /api/log_quality/reports?source_key=&rule_version=&version_state=`，
  `version_state` 取 `all|known|unknown`；占位文本（未标注 / 未知 / N/A 等）统一视为缺版本。
- **定位原日志**：`GET /api/log_quality/reports/{report_id}/evidence?include_lines=true`，
  返回行范围、偏移范围、内容摘要、证据快照路径，并给出快照是否存在。
- **对照筛选**：`GET /api/log_quality/comparison-candidates` 列出同源报告两两组合；
  `POST /api/log_quality/comparison/prepare` 核对被选中的两份报告，给出对照口径版本、
  是否可比、以及需要补证复算的报告编号。版本不同或都缺版本时不会猜测当前配置。
- **补证复算**：`POST /api/log_quality/reports/{report_id}/recompute`，入参可带
  `rule_version` / `rule_code` / `peer_report_id` / `interrupt_after`。复算只使用历史
  规则快照和报告固化的原始日志范围，明细写入 `data/exports/quality_recomputes.jsonl`，
  带证据摘要、规则快照摘要和来源报告编号。
- **中断保留待核**：`interrupt_after` 触发的中断会留下 `status=待核` 的明细，
  不计入趋势；也可用 `GET /api/log_quality/recompute-details` 单独查阅。
- **可复查趋势**：`GET /api/log_quality/trend?source_key=&rule_version=` 只用明确版本的
  原值和已完成的补证复算取数，未补证/中断的报告进入 `excluded` 并保留原值。

前端在「质量诊断」页底部的 **历史诊断对照与复算** 面板完成上述查询、勾选两份报告核对、
复算、查看明细与趋势。首次启动会自动写入一组演示历史报告（`QL_DEMO_20260901` 缺版本、
`QL_DEMO_20261120` 标注 `v1.2`），只写一次，不影响后续真实诊断。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
