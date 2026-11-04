# 社区"极简减负"智能体系统

> 本科毕业设计：基于 Multi-Agent 协作与本地大模型的社区"极简减负"智能体系统设计与实现

社区工作者用**大白话把一件事说一遍**，系统自动完成敏感信息脱敏、业务项拆解、跨业务线分发
（物业派单 / 政务台账 / 街道上报），做到"一次录入、多方复用"。

全程本地大模型推理：**推理只发生在内网的 GPU 上，数据不出内网**——这是本系统的硬约束，不是可选项。
（线上演示环境另算：云主机没有 GPU，靠一条加密隧道把请求转回本机推理，
此时请求会经过隧道，但**模型与数据始终在本机**，详见 `docs/16`。不要把它说成"无任何外网调用"。）

---

## 1. 它在解决什么问题

社区工作者日常面对的是"上面千条线、下面一根针"：一次入户走访里同时含人口登记、独居老人关怀、
楼道堆物隐患、下水道报修四件事，过去要分别在 4 个系统 / 4 张表里各填一遍，同一段话被抄 4 次。
系统把这个动作拆成两步：

1. **说一遍**：口述或粘贴一段话交给 FDE（Front Door Entry）首录台；
2. **系统分拣**：拆解 Agent 拆成结构化业务项，按去向自动写工单、台账、报表，并保留可审计的留痕。

量化效果（24 条真实语料 × 3 次重复评测，见 `docs/06`）：

| 指标 | 结果 |
|------|------|
| 录入次数 | 59 次 → **24 次**（−59.3%） |
| 漏派单率 / 误派单率 | 0% / 0% |
| 工单 F1 / 台账 F1 | 100% / 92.3% |
| 敏感信息召回率 / 库内明文残留 | 100% / **0 处** |
| 单条端到端耗时 P50 | 2.14 s |

---

## 2. 功能清单

| 编号 | 功能 | 入口 |
|:--:|------|------|
| F1 | FDE 首录（自然语言 → 原始记录） | `/` 首录台 |
| F2 | 拆解 Agent（7 类业务项 + 去向判定，JSON Schema 约束解码） | 随 F1 触发 |
| F3 | 保密审查 Agent（身份证/手机号强特征优先 + 字段三级密级） | 随 F1 触发 |
| F4 | 派单 Agent + 物业工单闭环（open→doing→done 状态机） | `/orders` |
| F5 | 填报 Agent + 台账中心（门牌特征查重，复用历史档案） | `/ledgers` |
| F6 | 报表 Agent（按自然月汇总，环比对比） | `/reports` |
| F7 | 督办 Agent（工单 SLA / 任务到期超时告警） | `/orders` `/tasks` |
| F8 | 审计日志（只追加、脱敏后落库、Agent 可筛选） | `/audit` |
| F9 | 三角色登录与 RBAC（社区 / 物业 / 街道，页面+API 双守卫） | `/login` |
| F10 | RAG 知识库（BM25 检索 + 本地模型带出处作答） | `/kb` |
| F11 | 任务下发与看板（街道 → 社区/物业，行级隔离） | `/tasks` |
| F12 | 字段三级管控（明文/脱敏/加密，`field_security` 可查可审） | 首录结果内展示 |
| F13 | 居民端：免注册填报 → 6 位查询码 → 进度查询 → 三级联系人 | `/portal`（手机优先，独立布局） |
| F14 | 公告发布：上级文件丢进 `data/notices/` 自动解析编排，双输出（打印版/发布版） | `/notices` · `/portal/notices` |
| F15 | 报表导出 Excel（6 表快照件：概览/业务线/类型/工单/明细/说明，零依赖生成） | `/reports` → 📥 |
| F16 | 收文办文（上级发文 → 签收 → **办结留痕** → 社区落地件 → 红头 Word / md / txt） | `/notices` |
| F17 | 公告置顶（3 条上限、置顶 ≠ 发布、满额明确拒绝） | `/notices` |
| F18 | 模型通道（线上经隧道回本机 GPU + 令牌网关；系统状态页可试跑与看调用记录） | `/system` · `/api/health` |
| F19 | 模型调用韧性（瞬态失败自动重试、失败归类留痕、keep_alive 抑制冷加载、僵死槽位回收、出网自检） | `/system` |
| F20 | 发文台（请示/报告/函 → 核稿 → 签发 → 用印 → 分发 → 归档，含密级与文号） | `/docs` |
| F21 | 工作周报（按自然周取数 → 模板成稿 → 人工补充 → 签发冻结） | `/weekly` |
| F22 | 三方仲裁（居民确认/不服 → 居委会裁决：物业责任退回重办 / 业主自用·公共部位维持办结；物业回避） | `/orders` → 流转记录 |
| F23 | 证据链存证（诉求/附件/流转逐步哈希上链，链头多方锚定；四种篡改可检出） | `/orders` 详情 · `/api/evidence/verify` |
| F24 | ABAC 访问策略（7 条规则 R1–R7、密级五档 × 角色上限、行级收窄与单条判定同源；规则可现场试算） | `/security/policy` |
| F25 | 导出水印（页面 SVG 平铺 / xlsx 页眉+末行 / docx 页眉，文案含姓名·单位·时间·动作，与审计成对） | 各导出入口 |
| F26 | 流程监控（每步停留时长按相邻日志差算；瓶颈排行带 N 值，样本不足显式标注；流程路径、卡点、异常清单五条可解释规则） | `/process` |
| F27 | 长尾风险预警（风险分可拆项显示；反复投诉从 2 次报；跨单位转派识别踢皮球；超 30 天未结；投诉热点词表 + 聚类纯度如实标注） | `/risk` |
| F28 | 多 Agent 协作与冲突消解（黑板 + 冲突检测 + 「更严者胜」裁定；轨迹逐步落库可回放；定级在建单之前，Agent 只执行不改结论） | `/trace` |
| F29 | 人员底册（台账格式来自街道下发的残疾人 / 防溺水 / 留守儿童四表；身份证哈希去重、姓名证件电话全脱敏；地址定位精度如实标注；报修地址命中重点人员户自动提级）。**演示库为 300 条合成数据**，真实台账已移出项目，原因见 `docs/35` §10 | `/persons` |

---

## 3. 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│ 表现层   Tabler 风格模板 + ECharts（首录台/工单/台账/报表/审计/看板）│
├──────────────────────────────────────────────────────────────────────┤
│ 接口层   Flask 蓝图 · RESTful /api/* · 统一 {code,msg,data}          │
├──────────────────────────────────────────────────────────────────────┤
│ 调度层   Orchestrator：保密 → 拆解 → 派单/填报 → 台账 → 审计         │
├──────────────────────────────────────────────────────────────────────┤
│ 智能体层 8 Agent（解析/脱敏/派单/填报/报告/督办/知识/复核）          │
│          统一继承 BaseAgent，自动写审计日志                          │
├──────────────────────────────────────────────────────────────────────┤
│ 模型层   Ollama + qwen3:8b（Q4_K_M）· 并发闸门 · 失效降级规则兜底    │
├──────────────────────────────────────────────────────────────────────┤
│ 安全层   RBAC 25 权限点 · 三级脱敏 · 审计只追加 · 字段密级落库       │
├──────────────────────────────────────────────────────────────────────┤
│ 存储层   SQLite(WAL) 开发 / MySQL 生产 · data/kb 知识库文件          │
└──────────────────────────────────────────────────────────────────────┘
```

目录：

```
bise4/
├─ app/
│  ├─ agents/          # 9 个协作单元：orchestrator + 8 个业务 Agent
│  ├─ api/             # 蓝图：fde/orders/ledgers/reports/tasks/audit/kb/health
│  │                   #       portal（居民端）/ notices（公告管理）
│  ├─ kb/              # 知识库：文件入库 + BM25 索引
│  ├─ notice/          # 上级文件解析(parse)/渲染(render)/入库发布(ingest)
│  │                   # + 收文办文(localize)/文档生成(export)/目录轮询(watcher)
│  ├─ portal.py        # 居民端页面（独立移动布局）
│  ├─ portal_seed.py   # 三级管理单位公开信息
│  ├─ llm/             # 唯一模型入口 client.py + 并发闸门 gate.py
│  ├─ templates/       # 页面模板（base.html 继承）
│  ├─ static/css/      # Tabler 风格样式
│  ├─ models.py        # 13 张表
│  ├─ auth.py          # RBAC / 登录 / 演示账号与任务种子
│  ├─ admin.py         # 账号治理（注册 / 待审核 / 用户与权限）
│  ├─ db_tuning.py     # SQLite WAL + busy_timeout
│  └─ views.py         # 页面路由
├─ config.py           # 全部配置走环境变量
├─ run.py              # 启动入口
├─ 启动系统.bat        # 一键启动（菜单：启动/自检/灌数据/停止）
├─ 停止系统.bat        # 停止服务
├─ data/               # community.db / kb 知识库 / **notices 上级文件投递口** / 实验 JSON
├─ docs/               # 00~36 设计与实验文档（口径、踩坑、审查记录）
└─ scripts/            # 启动自检、评测、压测、长稳、种子、出图脚本
```

---

## 4. 快速开始

### 4.1 前置条件

| 项 | 要求 | 说明 |
|----|------|------|
| Python | 3.10+（开发用 3.13） | 不需要 GPU 也能跑，只是会走规则兜底 |
| Ollama | 0.40+ | 本地模型运行时，[下载](https://ollama.com/download) |
| 模型 | `qwen3:8b`（Q4_K_M，4.87 GB） | 显存 ≥ 6.5 GB；不足可换 `qwen3:4b` |

### 4.2 安装

```bash
# 1) 依赖
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
pip install -r requirements.txt
pip install psutil matplotlib          # 仅跑压测/出图时需要

# 2) 本地模型
ollama pull qwen3:8b
ollama serve                            # 另开一个终端常驻

# 3) 灌演示数据（会清库重建：3 个账号 + 4 条任务 + 10 篇知识库 + 40 条首录）
python scripts/seed_demo.py --reset

# 4) 启动
python run.py                           # http://127.0.0.1:5000
```

### 4.3 Windows 一键启动

双击项目根目录的 **`启动系统.bat`**，菜单里选 `[1]` 即可：脚本会依次检查 Python、
依赖、Ollama、模型、端口占用，等服务真正起来（`/api/health` 通）再打开浏览器，
不会出现"点了链接却打不开"。

```
  [1] 启动系统       本地模型 + Web 界面，就绪后自动打开浏览器
  [2] 环境自检       不启动，逐项体检并给出修复建议
  [3] 重置演示数据   清空业务数据，重新灌入 40 条首录（约 10 分钟）
  [4] 停止服务       结束占用 5000 端口的进程
  [0] 退出
```

| 脚本 | 作用 |
|---|---|
| `启动系统.bat` | 菜单式启动器，15 秒无操作自动选 `[1]` |
| `停止系统.bat` | 结束 Web 进程，并询问是否顺带停掉 Ollama |
| `scripts\start_app.bat` | 旧入口，转发到根目录启动器（保留兼容） |

命令行也能驱动，方便写自动化脚本：

```bat
启动系统.bat --app       :: 直接启动，不显示菜单
启动系统.bat --check     :: 只做环境自检
启动系统.bat --seed      :: 重置演示数据（不再二次确认）
启动系统.bat --stop      :: 停止 Web 服务
启动系统.bat --ollama    :: 只确保本地模型服务在跑
```

> **两个坑记一下**：① 该 `.bat` 必须保存为 GBK(ANSI) 编码——存成 UTF-8 时 `chcp` 会让
> cmd 按字节重读文件，中文行会被截断成乱码命令；② 本机装了系统级 HTTP 代理，所有探测
> `127.0.0.1` 的 `curl` 都带 `--noproxy "*"`，Python 侧统一用空 `ProxyHandler`，否则会拿到
> 代理返回的 502。
>
> 换机器部署：项目内**没有任何绝对路径**，`DATABASE_URL`、`OLLAMA_BASE_URL`、`APP_PORT`
> 全部走环境变量；启动器用脚本自身位置定位项目根目录，整个文件夹可以搬走。模型仓库不在
> 默认位置时设 `OLLAMA_MODELS`，启动器也会自动兜底找 `F:\ollama-models`。

### 4.4 演示账号与居民入口

| 账号 | 密码 | 角色 | 落地页 |
|------|------|------|--------|
| `admin` | `admin123` | 系统管理员 | 用户与权限 `/admin/users` |
| `wang` | `123456` | 社区工作者 王社工 | 首录台 `/` |
| `wuye` | `123456` | 物业 李经理 | 工单中心 `/orders` |
| `jiedao` | `123456` | 街道 张科长（**上级单位**） | 任务看板 `/tasks` |
| — | — | **居民（第四类使用者）** | **`/portal` 免登录** |

> 管理员密码与其余三个不同，因为管理员能改所有人的权限——演示库密码是公开的，
> 用同一个 `123456` 等于任何拿到链接的人都能接管账号体系。
>
> 新同事可自助注册（登录页「新同事？申请工作账号」）：注册后是
> 「社区工作者 · 待审核 · 零权限」，管理员在「用户与权限」核对身份并勾选权限后才可用，
> 权限保存即时生效，对方不需要重新登录。详见 `docs/12-用户与权限管理.md`。
>
> **上级单位就是 `jiedao`**，不需要另外建角色。它能发文、下任务、看报表审计，
> 但不能签收、不能办文、不能对外发布——上级只发文和督办，不替社区办事。
> 街道/物业这类不会自己来注册的账号，由管理员在「用户与权限 → 新建账号」直接建
> （自助注册的角色固定是社区工作者）。详见 `docs/13-收文办文与文档生成.md` 第 2.1 节。

> 居民端不需要账号：填手机号即可提交，系统返回 6 位查询码，凭「手机号 + 查询码」查进度。
> 内部登录页底部有「我是居民」入口，居民端页头有「工作人员入口」，两边互通。
> 演示时建议用手机尺寸窗口打开 `/portal`，它是一套独立的移动端布局。

---

## 5. 配置项

全部通过环境变量覆盖，改完重启生效。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `sqlite:///<项目>/data/community.db` | 生产示例：`mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4` |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | 连不上自动降级为规则拆解 |
| `LLM_BASE_URL` | 空 | 覆盖模型地址（可指向公网隧道），优先级高于 `OLLAMA_BASE_URL` |
| `LLM_TOKEN` | 空 | 远程通道的网关令牌；本机直连不需要 |
| `LLM_MODEL` | `qwen3:8b` | 显存不足改 `qwen3:4b` |
| `LLM_NUM_CTX` | `4096` | 上下文窗口，越小越省显存 |
| `LLM_THINK` | `0` | Qwen3 思考链；实测耗时 ×9.1 且质量下降，默认关 |
| `LLM_MAX_CONCURRENCY` | `1` | 模型并发闸门；单卡串行推理，调到 2 无收益（见 `docs/07`） |
| `LLM_QUEUE_TIMEOUT` | `40` | 排队超时（秒），超时降级规则路径 |
| `LLM_KEEP_ALIVE` | `30m` | 模型驻留时长；Ollama 默认 5 分钟就把权重卸掉，低频使用等于每次冷加载 |
| `LLM_PROXY` | 空 | 远程通道的出网代理（`http://host:port`）；只在确实需要代理时填，不自动猜平台变量 |
| `SQLITE_WAL` | `1` | WAL 模式；关掉后读写互斥，吞吐 −62% |
| `SQLITE_BUSY_TIMEOUT` | `20000` | 写锁等待（毫秒） |
| `APP_PORT` / `APP_HOST` | `5000` / `127.0.0.1` | 压测脚本会用 `APP_PORT` 拉起独立实例 |
| `KB_EMBED_MODEL` | 空 | 填 `bge-m3` 启用混合检索（需额外显存） |
| `SECRET_KEY` | 开发占位值 | **生产必须换** |

---

## 6. 脚本

| 脚本 | 用途 | 典型命令 |
|------|------|----------|
| `启动系统.bat` | **一键启动**（菜单：启动 / 自检 / 灌数据 / 停止） | 双击，或 `启动系统.bat --app` |
| `停止系统.bat` | 停止 Web 进程（可选同时停 Ollama） | 双击，或 `停止系统.bat --yes` |
| `preflight.py` | 环境自检（Python/依赖/端口/模型/库/知识库） | `python scripts/preflight.py` |
| `open_when_ready.py` | 等服务就绪后再开浏览器 | 由启动器自动调用 |
| `seed_demo.py` | 灌演示数据 / 一键重置 | `python scripts/seed_demo.py --reset` |
| `smoke_auth.py` | 三角色权限冒烟（40 项断言） | `python scripts/smoke_auth.py` |
| `smoke_tasks.py` | 任务下发 + 字段密级（32 项断言） | `python scripts/smoke_tasks.py` |
| `smoke_person.py` | 人员底册：脱敏零残留 / 社区收窄 / 反查结论与派单同源 / 派单提级 / 导入幂等（61 项） | `python scripts/smoke_person.py` |
| `smoke_kb.py` | 知识库检索与问答（27 项断言） | `python scripts/smoke_kb.py` |
| `smoke_portal.py` | 居民端 + 公告全链路（80 项断言） | `python scripts/smoke_portal.py` |
| `smoke_security.py` | 越权 / 伪造 / 爆破 / Cookie（32 项） | `python scripts/smoke_security.py` |
| `smoke_security_hard.py` | 安全加固：响应头 / 健康分档 / 公告 scope 越权 / 查询码锁定 / 注册限流 / Cookie Secure / 导出留痕 + 输出转义守卫（62 项） | `python scripts/smoke_security_hard.py` |
| `smoke_admin.py` | 注册审核 + 建号 + 按人授权（71 项） | `python scripts/smoke_admin.py` |
| `smoke_notice_flow.py` | 收文 → 签收 → 办结 → 办文 → 导出 → 发布（86 项） | `python scripts/smoke_notice_flow.py` |
| `smoke_superior.py` | 上级单位账号 + 授权边界 + 督办视角（67 项） | `python scripts/smoke_superior.py` |
| `smoke_nav.py` | 页面返回链 + 待办计数一致性（26 项） | `python scripts/smoke_nav.py` |
| `smoke_pin.py` | 公告置顶：权限 / 排序 / 上限 / 置顶≠发布（48 项） | `python scripts/smoke_pin.py` |
| `smoke_stats.py` | 统计口径：页面数字 = 库内真实条数（50 项） | `python scripts/smoke_stats.py` |
| `smoke_report_export.py` | 报表导出 Excel：包结构 / 数字同源 / 快照不变 / 权限（72 项） | `python scripts/smoke_report_export.py` |
| `smoke_autoscan.py` | 公告自动扫描入库：目录监听、去重、幂等（13 项） | `python scripts/smoke_autoscan.py` |
| `smoke_llm_channel.py` | 模型通道：本机/远程判定、令牌网关、降级、观测接口与权限（51 项） | `python scripts/smoke_llm_channel.py` |
| `smoke_llm_resilience.py` | 模型调用韧性：瞬态重试、僵死槽位回收、失败归因、探活超时、keep_alive、代理只认显式配置（79 项） | `python scripts/smoke_llm_resilience.py` |
| `smoke_report_version.py` | 报表版本回滚 + 审计游标分页：一期一份不破、归档逐字节相同、版本号不重号、翻页不重不漏（82 项） | `python scripts/smoke_report_version.py` |
| `smoke_perf.py` | 传输层：gzip 分层、缓存分层、echarts 只在画图页、站内预取映射（57 项） | `python scripts/smoke_perf.py` |
| `smoke_rigor.py` | 严谨性：报表一期一份、发布幂等、入参类型、孤儿任务、JSON 404、跨站写、搜索分页（61 项） | `python scripts/smoke_rigor.py` |
| `smoke_order_flow.py` | 工单处理人与流转：处理人取会话身份、转派边界、事件链不改写、交接班（93 项） | `python scripts/smoke_order_flow.py` |
| `smoke_search.py` | 搜索语义：通配符当字面量、只搜内容不搜键名、2 字中文词、多词 AND、老库兜底、检索文本脱敏（48 项） | `python scripts/smoke_search.py` |
| `smoke_llm_badge.py` | 侧栏状态灯是真的：模型不通必须红、通了才绿、隧道口径、接口/渲染都不许同步探活（19 项） | `python scripts/smoke_llm_badge.py` |
| `smoke_boot.py` | 启动韧性：坏库自愈、无种子库降级启动、好库不误伤、`.reset_db` 幂等（18 项） | `python scripts/smoke_boot.py` |
| `smoke_mobile.py` | 手机适配：抽屉三件套、12 张表都在滚动容器里、模板不许留行内跨栏栅格、统计卡用栅格类、iOS 16px 阈值、弹窗不许默认展开（51 项） | `python scripts/smoke_mobile.py` |
| `smoke_logic.py` | 逻辑一致性：任务统计不受 limit 影响、任务写侧单位隔离、公告筛选是叠加不是替换、督办告警按身份收窄、台账归档算办结、两端置顶同键、老库回填幂等、管理员不被当成受派方（46 项） | `python scripts/smoke_logic.py` |
| `audit_probe.py` | 边界体检：空值 / 超长 / 类型错 / 越权 / 非法状态 / 分页边界（人工看报告，非断言） | `python scripts/audit_probe.py` |
| `sec_probe.py` | 信息安全体检：安全头 / 健康暴露 / 公告越权 / 查询码锁定 / 注册限流 / Cookie / 导出留痕 / 跨站写（打印现状，不预设结论） | `python scripts/sec_probe.py` |
| `check_integrity.py` | 数据层自洽：检索文本↔正文、工单状态↔流转链、办结时间、置顶时间、签收人、任务闭环时间、报表版本↔历史、居民单状态↔明细（只读，查副本） | `python scripts/check_integrity.py` |
| `bench_ledger_search.py` | 搜索方案实测：`LIKE` / FTS5 `unicode61` / FTS5 `trigram` 的命中和耗时对照（论证"为什么不用全文索引"） | `python scripts/bench_ledger_search.py --rows 50000` |
| `make_notice_demo.py` | 生成 .docx 版上级文件样例 | `python scripts/make_notice_demo.py` |
| `eval_parser.py` | 拆解质量评测（43 条标注集） | `python scripts/eval_parser.py` |
| `ablation_parser.py` | 消融实验（5 组配置） | `python scripts/ablation_parser.py` |
| `eval_e2e.py` | 端到端业务指标（24 条 × 3 次） | `python scripts/eval_e2e.py` |
| `bench_concurrency.py` | 并发压测（1→16 阶梯） | `python scripts/bench_concurrency.py --spawn --levels 1,2,4,8,16` |
| `soak_test.py` | 长稳测试（内存/显存/延迟漂移） | `python scripts/soak_test.py --minutes 30` |
| `plot_bench.py` | 压测与长稳出图 | `python scripts/plot_bench.py` |
| `plot_thesis.py` | 论文架构图/时序图/减负对比图 | `python scripts/plot_thesis.py` |
| `llm_gateway.py` | 本机模型网关：校验令牌后转发给 Ollama（公网隧道对端） | `python scripts/llm_gateway.py` |
| `start_llm_channel.py` | 一键起"网关 + 隧道"并写入新地址（线上模型通道） | `python scripts/start_llm_channel.py` |
| `audit_all.py` | **一键安全与流程体检**：凭据 / 会话 / 权限 / 前端 / 数据 / 线上共 29 项，有 FAIL 退出码 1 | `python scripts/audit_all.py`；线上 `--target ecs` |
| `backup_db.py` | 数据库备份：在线一致性快照 + 三关自检 + 轮转（可注册每日计划任务） | `python scripts/backup_db.py --install-task` |
| `rotate_password.py` | 换掉默认口令（默认只预演，`--apply` 才真正改并验证） | `python scripts/rotate_password.py --target ecs --apply` |

> 冒烟脚本各自带独立库（`data/smoke_*.db`，每次重建），直接跑不会动 `community.db`。
> 例外是 `smoke_tasks.py`：它依赖演示库里的字段密级数据，所以**复制**一份 `community.db`
> 到 `smoke_tasks.db` 再跑，原库同样不动。
> 评测脚本（`eval_*` / `bench_*` / `soak_*`）也都走自己的库，可放心跑。

### 上线 / 演示前的一次性检查

```bat
python scripts\audit_all.py     :: 六组 29 项，几秒出结果，有 FAIL 退出码 1
python scripts\backup_db.py     :: 先留一份可恢复的快照，再动别的
```

体检有 FAIL 就别对外开。当前唯一常态 FAIL 是「默认口令仍可用」——
演示期留着方便（`admin/admin123`），演示结束跑
`python scripts\rotate_password.py --apply` 换成随机串，新口令会写到
`data/_new_password.txt`（已忽略，不进仓库）。

想看线上而不是本机：`python scripts\audit_all.py --target ecs`（纯只读，
唯一副作用是验口令会写一条登录审计）。

---

## 7. 文档

| 文件 | 内容 |
|------|------|
| `docs/00-选题登记填写稿.md` | 选题登记四字段成稿 |
| `docs/00-开题报告填写稿.md` | 开题报告四项必填成稿 |
| `docs/01-总体设计.md` | 需求、角色权限矩阵、数据模型、Agent 契约 |
| `docs/02-模型部署与评测.md` | Ollama 部署、选型依据、推理参数 |
| `docs/03-评测与消融实验.md` | 43 条评测集、5 组消融（Schema 是主要增益来源） |
| `docs/04-角色权限实现.md` | RBAC 权限点与守卫、行级隔离 |
| `docs/05-RAG知识库.md` | BM25 检索设计、Top-k 与出处标注 |
| `docs/06-端到端业务指标评测.md` | 业务指标、缺陷复盘、减负口径公式 |
| `docs/07-并发与长稳测试.md` | 并发阶梯、瓶颈定位、事务边界缺陷、长稳曲线 |
| `docs/08-任务下发与字段密级.md` | 设计-实现漂移的补齐记录（含报表周期、SLA 分级两处口径修正） |
| `docs/09-演示脚本.md` | 8 分钟答辩动线、逐段话术、翻车预案 |
| `docs/10-部署形态与模型接入.md` | 公网发布与本地模型的关系、远程接入做法、答辩口径 |
| `docs/11-居民端与公告发布.md` | 第四类使用者、查询码设计、三格式解析、打印版与发布版、三个坑 |
| `docs/12-用户与权限管理.md` | 账号生命周期、三级权限来源、按人授权、防自锁、两个权限模型陷阱 |
| `docs/13-收文办文与文档生成.md` | 上级发文→社区签收→办文落地→导出红头 Word、收文状态与发布状态为何解耦 |
| `docs/14-统计口径与数据一致性.md` | 页面"总数"为何全错、统计必须服务端同源、布尔求和陷阱、测试污染演示库的整改 |
| `docs/15-报表导出Excel.md` | 为什么手写 xlsx 不用 openpyxl、6 表结构、快照冻结、手拼 OOXML 的 5 个坑、72 项测试 |
| `docs/16-模型通道与线上部署.md` | 线上没 GPU 时怎么真跑模型、隧道+令牌网关、两个反向的代理坑、瞬态重试与失败归因、线上库损坏事故复盘 |
| `docs/17-严谨性加固.md` | 以使用者身份审一遍边界：报表一期一份、发布幂等、入参类型、孤儿任务、JSON 404、跨站写、搜索分页，以及**明确没修的部分** |
| `docs/18-工单处理人与流转记录.md` | 为什么"单位"不能追责、字段与事件表分层、状态变更与留痕绑死、转派三条硬规则、处理时长口径 |
| `docs/19-报表版本与审计游标分页.md` | "一期一份"与"能回滚"为什么不冲突（双表解法）、回滚不重号、审计为何不能用 offset、同秒边界的漏记录 |
| `docs/20-信息安全加固.md` | 存储型 XSS 与全局转义、公告 scope 越权、健康接口分档、查询码锁定（审计表计数）、注册限流、安全响应头与 Cookie Secure 的两个时序坑、导出留痕 |
| `docs/21-状态可见性与启动韧性.md` | 侧栏"运行中"曾经是写死的常量、"数据不出内网"要跟通道走、后台探活缺 app context 的假故障、库坏了进程起不来导致发布失败、坏库自愈与降级启动、冷启动首调超时 |
| `docs/22-移动端适配.md` | 断点为什么选 900、侧栏改覆盖式抽屉、12 栏塌成两列（`grid-column:1/-1` 防隐式列）、表格 `max-content` + 列宽上限 + 内阴影提示、iOS 输入框 16px 阈值、`.modal.show` 的特异性坑、9 视口实测 |
| `docs/23-逻辑一致性审查.md` | 六个查出的逻辑缺陷（统计数了被截断的一屏、任务写侧漏隔离、公告筛选被整体替换、督办告警全表扫、台账归档不算办结、置顶两端不同键）+ 数据自洽检查 + 看到但没改的边界 |
| `docs/24-整体审查.md` | 十维体检清单（结构/配置/契约/状态码/权限/转义/审计/自洽/回归/文档）+ 三个"不报错但错了"的缺陷 + 9 条未改问题分级 + 批量跑测试不可信的真实根因 |
| `docs/25-前端请求入口与列表分页.md` | 40 处裸 fetch 收口到 `api()`（超时/非 JSON/断网三种失败才有提示）、4 个页面偷偷覆盖全局 `esc` 改回强转义、公告列表分页（越界夹取/非法参数/`matched` 与家底分离）、`_row()` 的 N+1、真浏览器验收（0 报错/桩数据翻页/断网提示条/390px 不溢出）、`smoke_frontend` 52 项 |
| `docs/26-工程踩坑总表.md` | 全部踩坑红线与索引：改代码前先查这里，别重复踩 |
| `docs/27-双端数据同步.md` | 本机 ↔ ECS 文件级同步、快照替换、只认显式请求的三个坑 |
| `docs/28-安全体检与运维自动化.md` | `audit_all.py` 六组体检、备份与改密脚本、pre-commit 钩子 |
| `docs/29-简历文案与数字核对.md` | 每个对外数字的复算命令、口径与定稿文案 |
| `docs/30-居务工作模块与流程设计.md` | 收文办文、周报、会议、督办的完整流程与状态机 |
| `docs/31-三方治理与证据链.md` | 仲裁闭环、多主体表态与证据链存证 |
| `docs/32-访问控制与水印.md` | ABAC 规则 R1–R7 的分层、密级上限、导出水印与审计 |
| `docs/33-流程监控与长尾风险预警.md` | 流程挖掘三条口径、长尾风险评分、投诉文本聚类 |
| `docs/34-Agent协作与冲突消解.md` | 黑板模型、真冲突判定与消解、"更严者胜"的取舍 |
| `docs/35-人员底册与真实台账接入.md` | 台账导入、地址精度分级、反查与派单联动、合成数据边界 |
| `docs/36-代码审查-性能与安全.md` | 性能与安全隐患分级、同步模型调用、代理头采信、状态码语义 |
| `docs/img/` | 论文用图（`fig1~4` 架构/时序/协作/减负，`fig7_*` 并发与长稳） |

---

## 8. 常见问题

**Q：模型没启动，系统还能用吗？**
能。`LLMUnavailable` 会触发规则兜底，拆解质量下降但链路完整（返回里 `llm_used=false`）。
这是刻意的降级设计——社区不能因为模型 OOM 就停摆。评测时请确认 `llm_used=true`，否则数据不可比。

**Q：显存 8 GB 跑 8B 模型爆了？**
先降 `LLM_NUM_CTX=2048`；仍不够改 `LLM_MODEL=qwen3:4b`。`.bat` 启动时 Ollama 会常驻，
多开其他吃显存的程序也会失败，可 `ollama ps` 查看占用。

**Q：`database is locked`？**
确认 `SQLITE_WAL=1`（默认开），且**不要**在数据库事务里调用模型。
`Orchestrator` 已把"落原文"和"写业务项"拆成两个短事务，写锁持有时间从秒级降到毫秒级；
如果你改动这一层，务必保持这个顺序（见 `docs/07` 第 5 节）。

**Q：怎么切 MySQL？**
`pip install pymysql`，设 `DATABASE_URL=mysql+pymysql://...`，首次启动会自动建表。
字段类型已按 MySQL 兼容选型，但**尚未在 MySQL 上跑过完整回归**，切换后请先执行三个冒烟脚本。

**Q：想换成自己的社区文件？**
把 `.md`/`.txt` 丢进 `data/kb/`，重启服务自动增量入库（按标题去重），然后在 `/kb` 页面检索验证。

**Q：公告怎么发？**
把上级单位的文件丢进 `data/notices/`（支持 `.md` / `.docx` / `.txt`），下次启动自动解析发布；
不想重启就用街道账号在 `/notices` 点「扫描目录并导入」。
**标题和发文单位缺一不可**，缺了会进"待确认"，由社区补全后手动发布——见 `docs/11`。
公告详情页可一键切「打印版」和「发布版」（红头公文）。

**Q：`llm_used` 一直是 false，但 `/api/health` 显示模型可用？**
先看本机有没有设系统代理。`requests` 默认会把发往 `127.0.0.1:11434` 的请求
也交给代理，代理回 404 而 `/api/tags` 恰好能通，于是"健康但不可用"。
代码里已用 `Session.trust_env = False` 绕开（`app/llm/client.py`），
若你自己新增了访问模型的代码，记得同样处理。

**Q：`/api/tags` 返回 `{"models":[]}`、`/api/chat` 报 404？**
Ollama 进程没拿到 `OLLAMA_MODELS`，去 C 盘默认目录找模型了。
停掉 `ollama app.exe` 与 `ollama.exe`，设 `OLLAMA_MODELS=F:\ollama-models` 后重新 `ollama serve`。

---

## 9. 已知限制

1. **吞吐上限由单卡决定**：端到端 0.62 请求/秒，并发只增加排队不增加吞吐（`docs/07`）。
   生产环境应改为异步受理 + 任务队列，这也是论文"未来工作"的第一条。
2. **台账 92.3% 而非 100%**：多建档来自拆分传导与跨线重复，真实重复为 0（`docs/06` 第 5 节有归因）。
3. **评测集 43 条为单人标注**，扩到 100 条 + 双人标注（Kappa ≥ 0.8）尚未完成。
4. **字段密级是"入库前不可逆脱敏"而非 AES 加密**：业务无需还原原文，不可逆强于可逆（`docs/08`）。
5. **MySQL 未实测**，见上。
