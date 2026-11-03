# 代办清单（一个功能一个功能做，做完打勾 ✅）

> 原则：先 demo 页面 → 逐个功能 → 部署本地模型 → 最后串联联调

## 阶段 0：骨架（当前）
- [x] 总体设计文档 `docs/01-总体设计.md`
- [x] Demo 静态页面 `demo/index.html`（FDE 首录台 + 看板 + 图表 + 审计日志，模拟数据）
- [ ] 确定最终技术栈版本（Python 版本 / Flask 版本 / 模型选型）

## 阶段 1：Flask 化（把 demo 变成真系统）
- [x] 搭建 Flask 项目骨架（app 工厂 + 蓝图：views / api(fde,tasks,orders) / agents / llm）
- [x] demo 页面迁入 Flask 模板（base.html + index.html），静态资源归位 static/css
- [x] 数据库模型落地 models.py（9 张表），开发用 SQLite，MySQL 留配置口
- [x] FDE 首录接口跑通：POST /api/fde → 保密审查(脱敏) → 拆解(规则桩) → 入库 → 审计
- [ ] MySQL 建库（生产切换，改 DATABASE_URL 即可）

## 阶段 2：核心业务功能（逐个实现）
- [x] **F1 FDE 首录接口**：接收自然语言文本，落库存原始记录
- [x] **F2 拆解Agent**：已接入本地 qwen3:8b，JSON Schema 约束解码（详见 docs/02）
- [x] **F3 保密审查Agent**：敏感字段识别（正则+模型双保险）+ 脱敏存储
- [x] **F4 派单Agent**：业务项含"物业"→ 自动生成脱敏工单（W+日期编号，SLA 24h）
- [x] **F4 物业工单页面**：/orders 工单中心（统计卡+列表+接单/反馈按钮），API 闭环 accept/finish（状态机校验+审计）
- [x] **F5 填报Agent**：政务类业务项自动写台账；门牌特征查重 → 复用历史档案（reused 标记）；工单完成自动归档物业台账
- [x] **F5 台账中心**：/ledgers 页面（统计卡+业务线筛选+复用/新建标记），API /api/ledgers + /api/ledgers/lines
- [x] **F6 报表Agent**：POST /api/reports/generate 一键汇总（首录数/业务线分布/工单办结率/复用数/脱敏批次/超期数 + 文字摘要 + 明细快照），/reports 报表中心页面（月份选择+生成+详情图表+历史列表）
- [x] **F15 报表导出 Excel**：`/reports/<id>/export/xlsx` 输出 6 表快照件（概览/业务线/业务类型/工单/明细/填报说明），零依赖手写 xlsx 写入器（`app/xlsx.py`），导出件与页面同源、库变化不影响历史报表；72 项回归 `scripts/smoke_report_export.py`（详见 docs/15）
- [x] **F18 模型通道**：`app/llm/channel.py` 统一裁决 base_url/token/model（env > llm.json > 默认），线上经 Cloudflare 隧道回本机 GPU；`scripts/llm_gateway.py` 校验 `X-LLM-Token` 后转发，避免 Ollama 裸奔公网；`/system` 系统状态页 + `/api/health/llm-test` 真实试跑；异常全局落审计 + `db-check` + 标记驱动库重置；47 项回归 `scripts/smoke_llm_channel.py`（详见 docs/16）
- [x] **F19 模型调用韧性**：远程通道对 `connect/timeout/5xx/bad_response` 自动重试一次（`queue` 与 4xx 不重试）；失败记 `kind`+`detail`（`last_error` 进 `/api/health` 与 `/system`，不含 prompt 正文）；请求体带 `keep_alive`（默认 30m，抑制 5 分钟卸载导致的反复冷加载）；**僵死槽位回收**（`max_hold=超时+30s`，排队超时前先回收，避免一次网络卡顿占住唯一并发位导致全站降级）；远程探活超时提到 25s（TLS 握手实测可达 9~26s，6s 会持续误报）；`GET /api/health/net` 出网自检（DNS/TCP/TLS/代理直连分开测）；代理只认显式 `LLM_PROXY`，不猜平台变量；79 项回归 `scripts/smoke_llm_resilience.py`
- [x] **F22 报表版本回滚 + 审计游标分页**：新增只追加表 `report_revision`（被替掉的每一版整份存档），`report` 仍是一行一期 → 「一期一份」与「能回滚」同时成立；回滚=用旧内容生成新一版（版本号不重号，`rollback_from` 记来源），`GET /api/reports/<id>/versions` + `POST /rollback`（挂 report.generate，街道只看不能改）；审计改 `(created_at,id)` 游标分页（offset 在只追加表上会重复+漏读），补 `ix_audit_created_id`，坏游标退首屏；「加载更多」改为追加；82 项回归 `scripts/smoke_report_version.py`（详见 docs/19）
- [x] **F21 工单处理人与流转记录**：`WorkOrder` 增 `assignee`/`assignee_id`/`assigned_at`/`done_at`，新增事件表 `work_order_log`（dispatch/accept/transfer/finish，只追加不改写）；状态变更与留痕绑死在 `app/orderflow.py:push()`，全站无裸 `o.status = ...`；处理人取会话身份（前端传名字无效）、转派限本单位在职且不动 `assigned_at`、交接班时接单人不被覆盖；详情页流转时间线 + 转派弹窗 + 按处理人搜索；93 项回归 `scripts/smoke_order_flow.py`（详见 docs/18）
- [x] **F20 严谨性加固**：报表一期一份（`Report.version` + 唯一索引，重新生成覆盖升版）、发布/下架幂等不改 `published_at`、首录入参类型与长度校验、任务受派单位真实性校验、`/api/*` 的 404/405 回 JSON、`Sec-Fetch-Site` 跨站写拦截、工单与台账的搜索+筛选+分页；体检 `scripts/audit_probe.py` + 61 项回归 `scripts/smoke_rigor.py`（详见 docs/17）
- [x] **F23 搜索语义（把 LIKE 的取舍做完整）**：先实测再决定——FTS5 默认 `unicode61` 把整句中文当一个 token（搜「电梯」命中 0/824），`trigram` 对 2 字词**静默返回 0 条**且索引是正文的 5.7 倍，要真分词只能引 jieba，那才真破坏"零依赖、可搬移"。所以维持 LIKE，但把它自身的坑修完：① `% _ \` 通配符转义 + `escape`（改前搜 `%` 返回**全表**）；② 新增 `Ledger.search_text` 只索引叶子值（改前搜一个 `_` 会靠键名 `room_key` 命中全表，搜 `type` 同理）；③ 多词 AND、全角空格也切、限 4 词；④ 检索文本是第二份拷贝，按 `mask_sensitive` 脱敏后再落库。`search_text` 由 ORM `before_insert/before_update` 自动维护（不靠调用方记得传参），老库一次性回填 + 查询侧 `coalesce` 兜底。48 项回归 `scripts/smoke_search.py`、实测 `scripts/bench_ledger_search.py`（详见 docs/17 §2.9）
- [x] **F24 信息安全加固**：以"使用者视角"写体检脚本打边界（`scripts/sec_probe.py`，首轮 27 项里 16 项不通过），修完 7 类：① **存储型 XSS**——41 处 `innerHTML` 中 7 个页面未转义，注册姓名→审计页、公文标题→知识库、FDE 原文→台账三条链可打到管理员会话；`base.html` 加全局 `esc()`，逐 sink 转义，并把"标题拼进 onclick"改成按 id 取；② **公告 scope 越权**——物业可列表看到、按 id 读到、导出 docx 拿走 `scope=community` 的内部件与 `pending` 草稿，`staff_query()`/`can_view()` 统一口径（列表/计数/详情页/导出四处共用）；③ `/api/health` 匿名泄露通道地址/pid/调用统计/db 报错，改匿名只给存活状态；④ 居民 6 位查询码**可枚举**（20 次/分钟 ≈ 35 天跑完），连错 5 次锁 30 分钟，计数落审计表（重启不清零）、键用手机号哈希；⑤ 自助注册无限流（6 连发 6 个账号），抽 `app/ratelimit.py` 按 IP 限 5 次/小时；⑥ 5 个安全响应头全缺 + `Server` 报 Werkzeug 版本，CSP/防点击劫持/nosniff 一次补齐，https 下补 Cookie `Secure`（**坑**：`after_request` 里加不上，Flask 存会话在其之后，得包 WSGI）；⑦ 导出公文/报表无审计，`_log_export()` 记操作人与文件名。62 项回归 `scripts/smoke_security_hard.py`（详见 docs/20）
- [x] **F25 状态可见性与启动韧性**：① 侧栏"本地大模型运行中"原来是 `base.html` 里写死的一行字（绿点跟模型毫无关系，卸载 Ollama 照样绿），改成 `llm_badge()` 单一出口——ok 绿 / bad 红 / idle 灰，首屏服务端渲染，`GET /api/health/llm` 每 20s 只读探活缓存（远程 TLS 9~26s 不能进渲染路径），后台线程补探（**坑**：线程里必须带 app context，否则 `channel.resolve()` 抛 RuntimeError 被吞，表现为灯永远"检测中"）；"数据不出内网"改为跟通道走（隧道时说"加密隧道回本机 GPU"）。19 项回归 `smoke_llm_badge.py`。② 发布时线上库损坏导致 `create_app()` 在 `tune_sqlite()` 的 `engine.connect()` 上抛异常 → 进程起不来 → 平台只报"没在端口响应"、链接直接打不开，而修库入口全在应用里（自锁）；改为 `_boot_database()`：文件级损坏才拿 `community.db.seed` 覆盖（先留 `.bak`、`dispose`、重试一次），否则**降级启动**，`[db][FATAL]` 打 stdout 首行 + 落 `data/boot_error.log`，健康接口加 `boot` 字段。18 项回归 `smoke_boot.py`（每个用例起独立子进程，同进程改 `DATABASE_URL` 改不动配置）。详见 docs/21
- [x] **F26 移动端适配（手机可用）**：原来的布局是给电脑配的——侧栏固定 230px + 正文 12 栏，375px 的手机上正文只剩约 145px；9 个页面 12 张表全无滚动容器，还被 `.card{overflow:hidden}` 裁掉，工单状态列和签收按钮直接看不见（不是难看，是办不了事）。① ≤900 断点：侧栏改覆盖式抽屉（`body.nav-open` 一个 class 驱动位移，不在 JS 里算尺寸，转屏/bfcache 都复位），正文塌成两列、卡片满宽、统计卡 2×2（`grid-column:1/-1` 压平跨栏，否则 `span 7` 会生成隐式列把整页撑宽）；② 表格：`.tw` 滚动容器 + `width:max-content` + 列宽上限 170px（用 `width:100%` 会把 13 位工单号切成两截、内容列折四行），容器内边缘阴影提示可滑；③ 触摸目标 40px、输入框 16px（**iOS 上 <16px 聚焦会整页放大且不还原**）；④ 弹窗手机铺满——**坑**：`.modal` 是 `display:none` 且写在各页 `<style>` 里，本文件更早加载、同优先级必输，写 `.modal{display:flex}` 会让所有弹窗一进页面就全开，只能挂 `.modal.show`；⑤ 清掉 25 处行内 `grid-column:span` 与 3 处行内多列栅格（**行内样式外部 CSS 压不动**，这是做适配的前提）；⑥ 刘海屏 `viewport-fit=cover` + 安全区、`theme-color`、`hover:none`、`prefers-reduced-motion`；不禁缩放（不写 `maximum-scale`）。实测 9 视口 × 11 页零横向溢出（320/360/390/430/横屏/768/912/1024/1440），51 项回归 `scripts/smoke_mobile.py`；视觉验收 `scripts/_shot_mobile.js`、`_shot_breakpoints.js`（playwright，不进回归套件）。详见 docs/22
- [x] **F27 逻辑一致性审查（六个静默缺陷）**：不抛异常、不白屏、日志干净，只有数字/状态/可见范围说错话的那一类。① `/api/tasks` 先 `limit(100)` 再 `len(rows)` 算 summary，133 条任务报 100 条（**统计必须服务端 count**）；② 任务列表按 `assignee_org` 隔离、办结接口只比角色，社区 A 可凭 id 办结社区 B 的任务（读写两侧都要，工单侧早就这么做了）；③ 公告列表 `recv=dispatched` 时 `q = todo_query()` **换掉**了整条查询，staff_query/status/kind 全丢——物业带这个参数能看到 scope=community 的内部件；改 `todo_filter(q)` 叠加，`counts.todo` 也改同源，否则卡片 4 条点进去 1 条；④ `/api/supervision/alerts` 挂 `order.view` 却全表扫，物业能看到别家工单与社区任务；`run(payload)` 按身份收窄；⑤ 居民单里只走台账（民政/综治）的事项 `filed != done`，永远显示「已受理」——归档就该算办结；⑥ 居民端置顶按 `published_at` 排、内部按 `pinned_at` 排，两条置顶件顺序相反；并把 `pinned_at` 回填改成每次启动幂等（早期 `_apply_pin` 只翻布尔值的行是在加列**之后**产生的，`if "pinned_at" in added` 永远命中不了）。新增 `scripts/smoke_logic.py`（34 项，A~G 分组）+ `scripts/check_integrity.py`（数据层自洽，只读查副本，首轮只报 1 条早期置顶缺时间）。全套 25 套 1288 项全绿。另有 ⑦ admin 被当成「受派方」：`_scoped()` 写的是黑名单（除街道外一律收窄），admin 被收窄成 `assignee_role=="admin"`——不存在的受派角色，**管理员工单页恒空、督办永远无告警**，而权限是满的；`notices.todo_filter()` 同样拿「平台运维」去比收文单位。三处统一改白名单（受派方只有 community/property），smoke_logic 加 [H] 组 12 项。详见 docs/23
- [x] **F28 前端请求入口 + 公告分页**：① 40 处各自写的 `await (await fetch(u)).json()` 收口到 `window.api()`——原写法有三种失败形态且**都不会在界面上说话**：断网 `TypeError`、网关 502 回 HTML 让 `r.json()` 抛 `SyntaxError`、没有超时则请求挂死；11 个页面一个 `catch` 都没有，表现统一是"表格停在加载中"，和"还没加载完"一模一样。`api()` 只把**通道层**失败归一化（超时 60s / 非 JSON / 断网）并弹顶部提示条，业务失败仍返回 `{code,msg,data}` 由调用方决定怎么提示（权限不足和网络断了不能混成一句）；提示条固定定位挂 `body`（放进 `.content` 会占掉 12 栏网格的一列）、`z-index:90` 高于手机抽屉 60、恢复后自动收起。② 顺带发现 4 个页面（kb/orders/tasks/notices）各自定义了同名 `esc()`，函数声明提升**覆盖**了全局强版本，只转 `& < >`（漏引号）——"整站 46 处都过 esc"在这 4 页实际是弱转义，全部删除。③ 公告列表 `limit(200)` 无分页，第 201 条静默消失而卡片仍是全量；加 `page/size`（字段名与 ledgers/orders 对齐），`page=99` 夹到最后一页、`size=0/abc` 回默认 50、`size` 上限 200，筛选只改 `matched` 不动家底。④ 顺手修 `_row()` 的 N+1：批量渲染时 `progress` 里没有 key 表示"没落地件"，原判断却当"没查过"，每条补查一次，一页 200 条就是 200 次。新增 `scripts/smoke_frontend.py`（52 项）+ 真浏览器验收 `scripts/_check_frontend_runtime.js`（10 页 0 报错、桩数据翻页、`route.abort` 断网弹条并自动收起、390px 不溢出）。全套 26 套 1335 项全绿。详见 docs/25
- [x] **F7 督办Agent**：SLA 超时扫描（工单 created_at+sla 超时、任务到期），/api/supervision/alerts；工单页超时红标+告警条
- [x] **F8 审计日志**：/audit 页面（Agent 筛选+摘要+外网调用0次计数），API /api/audit + /api/audit/agents
- [x] **F9 三角色登录与权限**：会话登录 / RBAC 权限点 / 页面+API 双重守卫 / 越权写审计 / 工单行级隔离（详见 docs/04）

## 阶段 3：本地大模型部署
- [x] 安装 Ollama 0.40.5（模型目录重定向 F:\ollama-models，GPU 加速已验证）
- [x] 拉取 qwen3:8b（Q4_K_M 4.87GB），实测热态 42.3 tok/s、显存 6.7GB
- [x] Python 侧 LLM 调用封装（think/num_ctx/temperature 可配，模型不可用自动降级）
- [x] 结构化输出：JSON Schema 约束解码 + 预分句校验重试（漏拆问题解决）
- [x] 模型效果评测脚本 scripts/eval_parser.py（**43 条**标注集：数量 97.7% / 类型 F1 99.0% / 去向 100% / 1.50s）
- [x] 消融实验 scripts/ablation_parser.py（5 配置对比，结论见 docs/03；Schema 是主因，思考链慢 9 倍）
- [x] 业务类型补第 7 类「隐患排查」（群租/私拉电线/消防通道，原先被迫塞进"调解"）
- [ ] 扩充到 100 条 + 双人标注（Cohen's Kappa ≥0.8）+ 3 次重复取均值
- [x] 端到端业务指标评测 scripts/eval_e2e.py（24 条 × 3 次：漏派 0% / 误派 0% / 漏建档 0% / 真重复 0 / 保密召回 100% / 库内明文 0 / 闭环 100% / P50 2.14s，详见 docs/06）
- [x] **F10 RAG 知识库**：data/kb 目录自动入库 + 中文字符二元组 BM25 检索 + 本地模型基于资料作答并标注出处；/kb 页面（问答/检索/文档增删，27 项测试全过，详见 docs/05）
- [ ] 知识库换真实社区文件后重跑检索评测（Top1/Top3 命中率、同义改写用例）
- [ ] 可选升级：`KB_EMBED_MODEL=bge-m3` 切混合检索（需显存余量），加 reranker

## 阶段 4：串联与收尾
- [x] Orchestrator 调度器：FDE→保密→拆解→派单/填报→台账→工单闭环→报表 已全链路串通（端到端评测验证）
- [x] 三角色登录与权限（社区工作者 / 街道 / 物业）→ 已提前完成，见阶段 2 F9
- [x] **并发与长稳测试** `scripts/bench_concurrency.py` + `scripts/soak_test.py`（16 并发零错误、落库零丢失；WAL 读写混合吞吐 +165%；发现并修复「模型推理期间持有写锁」的事务边界缺陷，详见 docs/07）
- [x] 全流程联调 + 演示脚本：`docs/09-演示脚本.md`（8 分钟动线、逐段话术、翻车预案、高频问答）
- [x] 论文素材整理：架构图、时序图、Agent 协作图、减负成效图（`fig1`~`fig4`）+ 并发 4 张（`fig7_1~4`）

## 阶段 5：答辩前必须补齐的缺口（2026-10-19 代码盘查发现）

- [x] **居民端（第四类使用者）**：`/portal` 免注册入口 —— 填报 → 6 位查询码 → 凭「手机号+查询码」查进度 → 三级管理单位公开信息；独立移动端布局（`static/css/portal.css`），与内部端双向互链（登录页「我是居民」/ 居民端「工作人员入口」）；`resident_ticket` 表 + 双因子查询 + 同 IP 限流 5 次/分；测试 `scripts/smoke_portal.py`
- [x] **上级文件自动发布**：`data/notices/` 投递口，支持 `.md`（YAML 元信息）/`.docx`/`.txt` 三格式解析 → 自动编排 → 发布；标题与发文单位缺一不可，缺失转「待确认」由社区补全；`notice.import`（街道）与 `notice.manage`（社区）两权分离
- [x] **双输出**：公告可一键切「打印版」（A4 规范版式）与「发布版」（红头 + 文号 + 仿宋三号 + 落款，按 GB/T 9704 主要视觉要素）
- [x] 修复**系统代理导致本地模型调用 404** 的隐蔽缺陷（`requests` 把 127.0.0.1:11434 也交给代理，健康检查却正常）→ `app/llm/client.py` 与 `app/kb/store.py` 改用 `Session.trust_env=False`

> 判定依据：实际代码/数据 vs 《总体设计》《开题报告》的承诺。原则是"论文写的必须能演出来"，否则改文档口径。

### A. 设计写了、代码里没有（最容易被问穿）
- [x] **街道端任务下发（任务看板）**：已补 `POST /api/tasks`（下发）+ `POST /api/tasks/<id>/done`（受派方确认 / 街道关闭）+ `/tasks` 页面 + `task.view`/`task.manage` 权限点；`task` 表补 `assignee_org/detail/creator/closed_at` 四列（启动时幂等 ALTER），并写入 4 条演示任务（含 1 条超时供督办演示）；街道默认落地页改为任务看板。测试 `scripts/smoke_tasks.py`（16 项）
- [x] **field_security 字段三级管控**：`SecurityAgent.classify_fields()` 按字段名 + 字段值双规则判级，Orchestrator 在业务项落库后写入 `field_security`；首录台展示每个业务项的字段密级与依据，`/api/fde/stats` 增加 `secure_count`；`docs/01` 第六节口径已改为"3 级字段入库前即不可逆脱敏，原文不落库"（比 AES 存储更强，且与代码一致）。测试 `scripts/smoke_tasks.py`（15 项）

### B. 论文素材
- [x] 架构图（分层）、FDE 业务时序图、Agent 协作关系图、减负成效对比图 → `scripts/plot_thesis.py` 输出 `docs/img/fig1_architecture` / `fig2_fde_sequence` / `fig3_agent_flow` / `fig4_benefit`（另并发相关 4 张 fig7_1~4）
- [x] 论文正文（7 份实验文档可直接改写为章节，素材已齐）→ 已成稿于 `thesis/`（10 个分章 + 合并稿，约 3.1 万字，8 张插图；`python thesis/build.py` 重建合并稿）
- [x] 论文 Word 排版 → `thesis/论文.docx`（82 页）：`python thesis/docx_build.py` 生成，`docx_finalize.ps1` 更新目录域并导出 PDF；封面/页眉页脚/罗马数字页码/三线表 37 张/表题 37 条自动编号/8 处 LaTeX 转 Word 公式/中文弯引号已就位。仅剩封面五个空与页眉校名需手填
- [x] 演示脚本与答辩动线 `docs/09-演示脚本.md`（含检查清单、逐段话术、翻车预案、10 条高频问答）
- [ ] 答辩 PPT + 录屏

### C. 工程可用性
- [x] README / 部署说明（含环境变量表、目录结构、脚本清单、常见问题）；`start_app.bat` 去掉 `F:\bise4` 硬编码，改为按 `%~dp0` 推导根目录，整个项目可搬到任意路径
- [x] **一键启动器** `启动系统.bat` / `停止系统.bat`：菜单式（启动 / 环境自检 / 灌演示数据 / 停止），自动找 Python→查依赖→拉 Ollama→确认模型→等 `/api/health` 通再开浏览器；配套 `scripts/preflight.py`（8 项体检）与 `scripts/open_when_ready.py`；支持 `--app/--check/--seed/--ollama/--stop` 参数；`scripts/` 下旧入口改为转发
- [ ] MySQL 切换实测（论文写"生产用 MySQL"，实际只跑过 SQLite）
- [x] 演示数据种子 + 一键重置 `scripts/seed_demo.py`：40 条首录（走真实 LLM 链路）→ 52 个业务项 / 17 张工单（三态） / 60 条台账 / 2 份月报 / 4 条任务 / 155 条审计，时间回填至过去 45 天（双峰分布，近 72 小时有集中活动）
- [x] 顺带修正两处"口径漂移"：报表未按 period 过滤（月报数字完全相同）、工单 SLA 一律 24h 与知识库《报修响应时限规范》矛盾 → 见 `docs/08` 第 6 节

### D. 可选加分（不影响答辩通过）
- [ ] 评测集扩到 100 条 + 双人标注（Cohen's Kappa）
- [ ] FDE 改异步受理（`docs/07` 已论证必要性，实现后是很好的"工程反思"章节）
- [ ] 语音录入（Whisper 本地版）→ 建议写入"未来工作"，不做

### E. 严谨性加固（2026-10-22 以使用者身份体检，详见 `docs/17`）
- [x] **报表一期一份**：`Report.version` + 库级唯一索引 `ux_report_period_line`，同月重新生成=覆盖升版；老库 14 条重复用 `scripts/_dedupe_reports.py` 清理（保留 id 最小、内容取最新、写审计）
- [x] **发布/下架幂等**：重复发布不再改写 `published_at`（发稿时间是证据，误触会把老文件顶到居民端最前面）；重复下架明说"已处于下架状态"
- [x] **首录入参**：类型判断前置（数字/对象不再 500）、5~2000 字、`source` 枚举校验
- [x] **任务受派单位校验**：必须是同角色真实存在的 org，否则拒绝并列出可选值（原来填错一个字任务就成孤儿，谁都看不到）
- [x] **接口契约**：`/api/*` 的 404/405 统一回 JSON（原来回 HTML，前端 `res.json()` 直接崩）
- [x] **跨站写防护**：`Sec-Fetch-Site: cross-site` 的写请求 403（不用 Origin/Host 比对——托管网关会改写 Host）
- [x] **分页参数**：`size=0` 不再被夹成 1 条（非正值视作没填）
- [x] **搜索与分页**：工单（关键词+状态筛选+上下页）、台账（关键词+业务线+上下页），筛选只改 `matched` 不改家底，条件放 URL
- [x] 新增 `scripts/audit_probe.py`（体检）与 `scripts/smoke_rigor.py`（61 项回归），全套 17 套 863 项
- [ ] 工单缺"处理人"字段（现在只能靠反馈文本追溯谁办的）
- [ ] 居民端查询码连续错误锁定（现在只有 20 次/分钟限流，10⁶ 空间可枚举；真实上线必须加）
- [ ] 审计日志分页（1129 条靠筛选还能用，过万要游标分页）
- [ ] 报表回滚到上一版（与"一期一份"取舍冲突，暂不做）

### F. 数据互通（**ECS 侧已实施**；沙箱侧仍暂缓）

> **2026-10-23 更新**：本机 ↔ 阿里云 ECS 用「文件级快照同步」做掉了，
> `scripts/ecs_sync.py push|pull|status` + 服务器每分钟兜底任务 `ca-sync`，
> 详见 `docs/27-双端数据同步.md`。下面这套基于 HTTP 接口的方案**只对线上沙箱仍然成立**，
> 沙箱只有一个端口、没有 SSH，才需要自己写接口 + 防呆；ECS 有文件系统，scp 一个文件就够了。
> 若哪天不做 ECS 了、要回到沙箱，再按下面这份清单做。

背景：线上沙箱只给一个 HTTP 端口，**不提供外部数据库**，声明 MySQL/Redis 依赖会被直接拒发布；
SQLite 又是单文件、没有服务端，两端不可能"连同一个库"。所以"互通"只能是搬运或收敛写入。

已定的路线（**不要再走行级合并**，那要 id 分段 + 全表 `updated_at` + 墓碑 + 冲突清单，约 1100 行）：

- [ ] **自动镜像**：本机为写入端，`sync_daemon` 每 5s 检测库变化并推送，线上跟随端接收后覆盖。
      比赛演示时"本机投屏操作 → 评委手机上的线上链接同步变化"，零手工步骤。
- [ ] **覆盖用「事务内整库覆盖」而不是替换 .db 文件**：几百行数据 DELETE+INSERT 毫秒级，
      不碰文件就绕开了连接池持有旧 inode、Windows 文件占用、"能打开但数据是旧的"这一整类问题；
      WAL 下读者只会看到覆盖前或覆盖后，失败整体回滚。
- [ ] **防呆**：覆盖前比对审计指纹 `(created_at, agent, action)`，本端有源端没有的行
      = 跟随端被人写过 → 拒绝覆盖并列出是哪几条；`agent=dbsync` 自身留痕要排除，
      否则第二次同步会被自己上次的审计挡住。
- [ ] **收尾必做**：`kb_doc` 换了必须 `kb.store.rebuild()`，否则检索继续用旧索引回答，
      页面看不出任何异常（最容易漏的一步）。
- [ ] 导出/接收接口都要 `admin` + `user.manage`；快照用 `sqlite3.backup()` 取一致状态，
      不能拷主文件（库开着 WAL，直接拷会丢未 checkpoint 的事务）。
- [ ] 一键启动 `scripts/start_demo.py`：网关 + 隧道 + 同步守护，避免答辩漏起进程。

已知代价（要向评委讲清，不是缺陷）：写入必须锚定一端，另一端只读；
断网时线上照常跑自己的库、只是停止更新。

## 待确认问题 ❓
1. ~~开发机器配置~~ ✅ 已确认：RTX 4060 Laptop 8GB 显存 + 16GB 内存 → qwen3:8b
2. ~~Python 版本~~ ✅ 已确认：项目独立 venv（Python 3.13），不污染系统 3.7
3. 物业端是单独页面还是简化视图？
4. 语音录入是否作为必需功能（可加 Whisper 本地版，工作量大）？
