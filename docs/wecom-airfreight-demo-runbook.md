# 空运本地 Demo 操作说明

当前管理层演示已采用双链路人工审核首页，请先阅读 [双链路演示说明](management-demo.md)。本文以下内容描述原详细 A/B 工作台，入口改为 `/workbench`，不作为本次真实聊天与 HKD 价表的默认演示流程。

## 启动

在项目根目录执行：

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
& '.venv\Scripts\python.exe' -m chatlog_assistant wecom-local serve `
  --analysis-db 'data\wecom-airfreight-demo\analysis.db' `
  --source-analysis-db 'data\airfreight-local-source\analysis.db' `
  --host 127.0.0.1 --port 8880
```

打开 <http://127.0.0.1:8880/workbench>。`--analysis-db` 是 Demo 的隔离业务库；每个 `--source-analysis-db` 都只是流程 B 可发现的规范化本机来源库，可重复指定。默认页面只展示空运业务；旧揽收页面在 `/legacy/pickup`，用于只读兼容检查。

### 准备合法本机来源

如当前来源库没有目标日期，可在已授权的 Windows 用户上下文中使用既有解析器生成**新的本机规范化快照**，不要对企微原始库做写入：

```powershell
& '.venv\Scripts\python.exe' -m chatlog_assistant wecom-local capture-once `
  --account-id <已发现的本机账号> --full `
  --conversation-name '中技AI cosplay' `
  --analysis-db 'data\airfreight-local-source\analysis.db'
```

随后将该新快照作为 `--source-analysis-db` 传入。页面先仅读取来源元数据；只有在流程 B 第 4 步明确勾选确认后，选定的账号、快照、会话、时间范围和预览摘要才会写入 Demo 隔离库。页面默认也提供明确标注的合成演示数据，便于非技术人员完成全流程讲解；它独立于真实来源，不能标为真实聊天或正式报价。使用 `--no-demo-fixtures` 可隐藏该演示数据。

## 给非技术人员的展示方式

页面按步骤推进，当前步骤会先说明“发生了什么”和“下一步做什么”。结果区默认只显示业务结论、关键数字和处理提示；文件指纹、字段编号、原始范围编号等技术信息放在“查看技术详情（可选）”中，需要追溯时再展开。给非技术人员展示时，可在流程 B 第 2 步选择“合成演示数据（非真实）”；若先选真实来源而在第 5 步遇到完整性阻断，可点击“改用合成演示数据继续”。这会建立一条独立导入，保留原真实导入，并在消息、成果卡与报价预览中持续显示合成标记。

## 流程 A：价卡解析与发布

1. 点击“创建／读取混合演示批次”，确认 3 条供应商回复按顺序出现，并承载 PDF、XLS、XLSX、DOCX、CSV、PNG 和损坏文件共 8 个真实本地附件；可用“打开原件／下载”验证二进制文件。
2. 点击“配置演示审核人”，再按步骤执行到“人工审核”。演示中的 @供应商和回复都是本地事件，不写真实企微。
3. 在“待审核与证据”中选择原始候选解决同键冲突；原始观察值和字段证据仍保留。
4. 执行“发布最新价卡”和“查看发布结果”。主舞台会显示当前运行生成的完整发布价卡单，包含航司、路线、重量档、币种／单位、有效期、发布状态、来源入口和 PDF 下载。未配置审核身份或仍有开放冲突时，服务端拒绝发布。

## 流程 B：群聊导入与报价预览

1. 切换到流程 B，先读取本地来源；默认搜索为“中技AI cosplay”。同名会话按账号、快照和会话 ID 分开显示；“合成演示数据（非真实）”是独立可选范围。
2. 明确选择来源和时间范围，先生成只读完整性预览，再点击“确认导入并分析”。如果预览内容和刚才的选择不一致，系统不会导入。
3. 第 5 步按需展示目标外层“群聊的聊天记录”、完整嵌套树、相邻上下文、转发缺口和附件状态；演示运行状态不会复制聊天正文。
4. 如果目标根消息、嵌套转发或附件不完整，服务端在第 5 步阻断真实链路，明确要求补齐合法本机来源后重新预览。演示者也可明确选择“改用合成演示数据继续”；系统会保留真实导入，建立独立的合成范围，且绝不把它标为真实场景完成。
5. 真实完整性通过，或已明确选择合成演示范围后，才继续解析多票截图、处理低置信度 CTN／单件语义、执行计费重、价卡匹配和内部测算。第 9 步会在每票命中结果下展示提供该费率的原始价卡附件；附件支持打开、下载，PDF 可直接内嵌预览。
6. 在第 11 步显式勾选确认将 `+ USD 0.20/KG` 应用于本次报价。客户单只显示最终销售单价，基础供应商成本仍只在内部测算中可见。
7. 最后按独立询价票分别生成英文报价单和各自 PDF。合成范围应看到 2 张匿名客户报价、共 4 个选项；抬头为 `Dear Customer:`，蓝色表格列为 `Option / Airline / A/F / Routing / Frequency / T/T`，有效期为生成日加 7 天，并持续标记 `DEMO — NOT SENT`。

## 主要接口

所有接口均为本机 HTTP。读取接口包括 `/api/airfreight/meta`、`/sources`、`/chat/preview`、`/batches`、`/rate-cards`、`/conflicts`、`/quotes` 和 `/quote-preview`；发布价卡 PDF 使用 `/rate-cards/{version_id}/published-sheet.pdf`，逐票报价 PDF 使用 `/quote-preview/{quote_id}/quotation.pdf`。写接口包括 `/demo/step`、`/batches`、`/chat/import`、`/quotes/parse`、冲突解决、价卡审核和报价确认。写请求沿用同源 Host／Origin、CSRF 和服务端审核身份校验。

`POST /api/airfreight/demo/step` 必须带 `flow`、`action`、`state_version` 和 `idempotency_key`。`execute`/`retry` 执行业务动作；`continue` 只推进状态，不重跑已完成动作；`select`、`set_mode` 只改变演示轨迹。流程 B 第 5 步只有在演示者明确提交 `use_synthetic_demo: true` 时才会切换到独立合成导入。服务端以 `demo_run` 保存当前状态、以 `demo_step_event` 追加记录执行、阻断、失败与重试；刷新页面可恢复，重置只重置轨迹，不删除批次、来源、字段证据或人工决定。

## 格式与边界

| 格式 | 本地路径 | 失败边界 |
| --- | --- | --- |
| PDF、XLS/XLSX、DOCX、CSV | 原生结构优先 | 损坏、扩展名不一致、宏／外链／脚本或缺少字段时拒绝或转人工 |
| JPG/JPEG/PNG | 本地 OCR／布局路径 | 当前环境无通用 OCR 时稳定返回 `needs_manual_review` 和原图证据；合成 PNG 夹具使用内置可重复基线 |
| DOC、旧版 XLS | 安全本地转换器 | 未配置转换器时不执行 Office，稳定转人工 |

演示 `÷6000` 只是一条明确标注的本地规则；`+0.20/kg` 先保存为待确认内部候选，仅在 B11 人工显式确认后形成销售单价；歧义附加费不计入合计。原始价卡、内部测算和报价选项分别保存。重置只重置演示状态，不删除原始证据。

## 验证与恢复

```powershell
& '.venv\Scripts\python.exe' -m unittest tests.test_wecom_airfreight -v
```

数据库初始化使用追加式 schema migration：旧揽收表和旧页面不改写。若浏览器仍显示旧页面，停止该本地服务后用相同 `--analysis-db`／`--source-analysis-db` 参数重启，再刷新页面；不要删除真实来源库来“重置” Demo。
