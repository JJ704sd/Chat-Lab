# 企微公司、价格与聊天证据操作说明

本说明对应 [公司信息、最新价格维护与聊天证据改版规格](wecom-company-pricing-evidence-spec.md)。实现保持本地优先：不修改个微页面、原始企微采集／解密机制或原始消息库。当前完成边界见 [本期验收与限制](wecom-pickup-acceptance.md)：聊天窗口已做真实页面验证，价格与 Excel 暂以合成验收为准，未执行真实价格回填或外部模型调用。

## 启动本地页面

使用项目已有的 `wecom-local` 入口，并明确指定分析库：

```powershell
uv run chatlog-assistant wecom-local serve --analysis-db data/wecom-local/analysis.db --port 8766
```

页面默认只监听 `127.0.0.1`。先在页面顶部限定账号、来源库和业务会话；公司修正、价格提交、Excel 预览确认和停用都必须带完整范围。页面写请求还需要服务端发放的 CSRF token、同源 `Origin`／`Host`，服务端不采信浏览器提交的操作者身份、自动批准标记、来源认证或跨范围字段。

首次使用前可在本地配置价格审核负责人；价格审核 LLM 仍默认关闭：

```powershell
uv run chatlog-assistant wecom-local prices-config --analysis-db data/wecom-local/analysis.db `
  --reviewer-id OPERATOR_ID --reviewer-name "运营审核人"
uv run chatlog-assistant wecom-local prices-config --analysis-db data/wecom-local/analysis.db --price-llm off
```

`--price-llm on` 只是显式记录独立开关和模型标识；没有已配置的价格审核适配器时不会外发，候选会保留并转人工。开启前应先由用户确认脱敏传输范围。

页面主要区域：

- 默认“价格明细”合并展示现行价格与未生效候选；“最新价格”展示有效记录，“待完善”和“待审核”按状态筛选。字段来源位于报价明细；聊天窗口以消息气泡、引用和转发卡片展示证据，不展示技术来源详情。
- “业务轮次”是轻量索引；“全量已导入消息”独立检索和分页，首屏不加载全部正文。范围总数、匹配数和已加载数分别显示。
- “公司身份”可在限定范围内预览和提交人工纠正。显示层覆盖不会改写原始 `messages` 身份；冲突会停留在待确认状态。

## 价格语义

七个核心列固定为：`起点`、`终点`、`重量`、`体积`、`包装数量`、`时效`、`价格`。维护行同时保留报价公司、单位、币种、计价方式、报价时间、状态、审核人、来源范围、来源消息 ID、`record_id`、`base_version` 和模板版本等元数据。

匹配键在同一账号、来源库和业务会话范围内按报价公司、路线、明确的重量／体积／包装条件、包装类型、服务时效、币种和计价方式等字段隔离。未知值不作为零、通配符或“不适用”；无法确认公司、币种、单位、计价方式或条件的候选不能自动采用。没有有效期的报价不被承诺为永久有效。

报价业务时间决定新旧。导入时间、审核时间、页面刷新时间和解析重建时间不会把旧报价变新。旧候选、重复提交、旧消息重导和重建分析都不能覆盖更新的现行值或人工纠正；待审核、驳回和冲突不覆盖现行值。价格变更与审计在同一事务中提交，重复请求使用幂等键，版本不匹配返回冲突。

## 人工审核与模型建议

责任岗位默认是“价格审核负责人（运营／报价管理岗）”，可由本地配置指定具体人员。人工确认、修改和停用必须提供已配置的身份、版本和理由，并记录确认人、时间、结论、前后值及证据。系统规则自动采用会标记系统执行、规则版本和证据，不冒充人工确认。

价格审核 LLM 是独立开关，默认关闭；已有其他模型密钥不会自动开启。开启前必须明确脱敏传输范围，模型只能返回可核验的建议、引用和疑点。引用越界、非法输出、失败或超时都会保留候选并转人工；模型不能改主体、直接写价格或绕过硬规则。测试只使用替身适配器。

## Excel 模板、导入与导出

下载模板：

```powershell
uv run chatlog-assistant wecom-local prices-template --output .\exports\wecom-prices-template.xlsx
```

导出全部当前筛选范围（不受页面当前页限制）：

```powershell
uv run chatlog-assistant wecom-local prices-export `
  --analysis-db data/wecom-local/analysis.db `
  --account-id ACCOUNT_ID `
  --source-database SOURCE_DB `
  --conversation-id CONVERSATION_ID `
  --output .\exports\wecom-prices.xlsx
```

读取价格 JSON：

```powershell
uv run chatlog-assistant wecom-local prices `
  --analysis-db data/wecom-local/analysis.db `
  --account-id ACCOUNT_ID `
  --source-database SOURCE_DB `
  --conversation-id CONVERSATION_ID `
  --view current
```

网页 Excel 导入流程是“上传并预览 → 逐行检查差异、重复键和错误 → 明确选择有效行 → 进入统一审核”。旧预览、过期预览或 `base_version` 冲突不会覆盖新状态；Excel 内伪造的审核人、状态、来源认证和自动批准字段不生效。系统拒绝或隔离宏、公式、外链、隐藏行列、合并单元格、超大文件和超限 XML；不会执行工作簿内容。导出会对可能被 Excel 解释为公式的文本加安全前缀。

工作簿包含“价格维护”和“填写说明”两个工作表，单行表头、冻结首行、筛选和合理列宽。七个核心列排在最前，其余列用于单位／币种、审核和证据回溯。

## HTTP 读写入口

本地页面使用以下入口：

- `GET /api/wecom/config`：责任岗位和价格审核 LLM 开关状态。
- `GET /api/wecom/prices`：`view=details|current|incomplete|pending|all`，支持公司、路线、关键词和范围筛选；CLI 仍提供 `current|pending|all`。
- `GET /api/wecom/price-detail` 与 `/api/wecom/price-export`：在明确范围内读取或导出选中的现行价格／候选；单条展示文件的主表仅七字段和一条数据。
- `GET /api/wecom/prices-export`：支持 `template=1` 或同样的范围筛选，循环读取完整结果后生成 `.xlsx`。
- `POST /api/wecom/price-candidates`：统一创建聊天、网页或 Excel 候选；网页请求会被服务端强制标为人工来源。
- `POST /api/wecom/price-candidates/{candidate_id}/review`：审核、驳回或修改候选。
- `POST /api/wecom/prices/{record_id}/deactivate`：人工停用现行记录并保留证据。
- `POST /api/wecom/prices-import/preview` 与 `/api/wecom/prices-import/confirm`：Excel 预览和确认。
- `GET /api/wecom/messages`：独立的全量已导入消息检索；默认只返回摘要，不返回正文。
- `GET /api/wecom/messages/{message_id}/evidence`：按范围读取目标消息、父级／回复祖先、引用和证据缺口。
- `GET /api/wecom/messages/{message_id}/forwarded`：在原证据范围内读取转发子消息，保留原作者身份。
- `GET /api/wecom/companies`、`POST /api/wecom/company-corrections`（`mode=preview` 或提交）：公司身份查看、预览和有范围纠正。

错误响应包含稳定的 `error_code` 和可读消息；版本不匹配使用明确的冲突错误。GET 只读，不触发生效、外部调用或价格写入。

## 迁移、备份与恢复

价格流程的自动化验证使用临时 SQLite 和合成数据；真实聊天 UI 验证先备份分析库，启动新版服务后核对既有消息、分析和维护记录未改变。后续真实数据库迁移需另行授权，按以下顺序执行：

1. 停止所有旧版和新版写入者，确认只有一个迁移／写入进程。
2. 在同一文件系统对分析库做带时间戳的 SQLite 备份，并用 SQLite 校验和抽样查询验证备份可读。
3. 先在备份副本上执行初始化和增量迁移；只新增表／字段和索引，不删除旧字段，不改写原始消息。
4. 按账号、来源库和会话分范围幂等迁移，记录游标、批次、成功数和错误；失败批次保留现场后重试。
5. 用恢复副本演练恢复路径，再逐项验证旧 report 完整性、index 轻量语义、`routes.quotes` 原义、事件 CSV 32 列、价格键隔离和审计原子性。
6. 未完成核验前不恢复覆盖真实库；不同时运行旧版和新版写入同一分析库。

恢复时保留原始库和迁移日志，先把备份复制到新的工作路径，在副本上验证恢复，再由用户明确决定是否切换。未授权前不执行删除、收缩迁移、重采集、重启企微、真实模型外发或部署。

## 验证边界

当前测试覆盖合成消息、嵌套转发、公司冲突与显示覆盖、价格公司／时效／条件隔离、缺字段阻断、旧报价不回滚、幂等和版本边界、同源 CSRF、独立消息分页、证据父级边界、LLM 替身引用校验以及 `.xlsx` 往返和文件安全。测试结果和未运行的真实库／外部调用检查以本次交付报告为准；通过测试不等于真实云端历史完整或真实数据完整性已证明。

## 合成规模实测（2026-09-03）

在临时 SQLite 中生成 10,000 条同秒长中文消息，包含 5 层父级链和 11 条未解析媒体标记；使用 500 条分页遍历：写入 0.223 秒，20 页检索 1.053 秒，范围总数与唯一消息数均为 10,000，首屏未返回正文；轻量报告索引 0.093 秒，深层证据读取到 4 条父级。该结果是本机合成数据实测，不是 SLA，也不代表真实数据规模或云端完整性。
