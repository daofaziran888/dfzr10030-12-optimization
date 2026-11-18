# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 通道恢复续传 | `recovery` | 通道恢复偏移及续传清单 | 失败偏移、恢复偏移、原文件/修订版本、续传分片 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 通道恢复偏移及续传清单（断点续传）

接入通道导入中断后不再整批重放，而是按失败偏移只补传未完成分片，已完成分片不会再次进入解析。
领域规则集中在 `backend/app/services/recovery.py`，接口前缀 `/api/recovery`，前端页面为
`frontend/src/views/recovery/index.vue`（侧边栏「通道恢复续传」）。

关键约定：

- 续传清单同时记录原文件、通道编号、失败偏移/恢复偏移与每个分片的偏移区间；失败文件保留
  错误位置（字节偏移 + 行号）。
- 并发确认通过提交令牌（client_token）幂等：同一令牌只生成一份清单；同一清单重复确认仅第一份
  生效，第二份直接拒绝且不改写清单状态。
- 中段失败且供应商上传修订版本时支持两种做法，生成不同清单：
  - 原文件偏移前置（`original_first`）：原文件身份前置决定续传边界，原批次偏移连续直接可用，
    旧解析结果保留可回查；
  - 修订版本前置（`revision_first`）：按版本边界切换修订文件并重新计算剩余分片偏移，由修订版本
    前置决定剩余数据内容。
- 无论哪种做法，恢复完成后每个输入分片有且仅有一份可解析结果（以「来源文件 + 分片序号」为唯一
  键，重复续传不重复写入）。
- 格式解析服务按续传清单只读取未完成分片生成字段包，核对清单后把可用状态同步回数据接入通道；
  相邻业务面通过待核对台账（`/api/recovery/ledger`）跟随清单状态联动更新。
- 清单支持上传、导入校验（失败清单返回逐处错误位置）与 CSV 导出下载，导出后可直接再次提交。

领域：数据工程
子方向：数据采集、爬虫与RPA
