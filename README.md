# 物流聊天记录分析助手

一个本地优先的企微/个微聊天分析 MVP：

- 从发送者显示名末尾的 `@主体` 标签识别主体；`@中技`、`@中技物流` 归为“中技”，其余归为“其他”。
- 识别物流问题类别，包括询价、提送货、时效航线、订舱、报关清关、轨迹、异常、理赔、账单、包装与货物规格。
- 可选调用国内 **MiniMax-M3** 对关键词分不准的消息做语义识别（含人设、提示词、任务分发等）。
- 将后续回复与问题关联，并区分“即时响应”和真正的解决方案。
- 将结果保存在本机 SQLite 数据库，通过只监听 `127.0.0.1` 的网页查看。
- 发现并监控本机企微/个微加密消息库的变化。
- 个微 4.1+ 通过只读 `Config.Cipher` 对象扫描与 HMAC / `sqlite_master` 双重验证恢复密钥；企微 5.x 按账号独立验证 wxSQLite3 AES-128。
- 通过双重验证的密钥只写入本机 Windows DPAPI 存储，不落明文配置文件。

## 快速开始

```powershell
uv run chatlog-assistant init
uv run chatlog-assistant import-jsonl samples/sanitized_chat.jsonl
uv run chatlog-assistant serve
```

然后打开个微 <http://127.0.0.1:8765> 与企微 <http://127.0.0.1:8766>。

查看本机数据源：

```powershell
uv run chatlog-assistant discover
uv run chatlog-assistant watch --once
```

在已确认合法授权后，只读探测当前登录个微/企微的数据库兼容性，并把通过验证的密钥写入 DPAPI：

```powershell
uv run chatlog-assistant probe-wechat --save
uv run chatlog-assistant probe-wecom --save
uv run chatlog-assistant import-live
uv run chatlog-assistant watch --once
```

探测器只报告是否命中与表数量，不显示明文密钥。

当前本机实测：微信 **4.1.12.55** 与企微 **5.0.10.6025** 在只读内存扫描下未能通过 HMAC / 页头双重验证。已授权时可用调试器硬件执行断点在登录阶段捕获密钥（不注入未签名 DLL、不修改微信/企微磁盘文件）：

```powershell
uv run chatlog-assistant probe-wechat --save --capture --restart
uv run chatlog-assistant probe-wecom --save --capture --restart
uv run chatlog-assistant import-live
```

`--restart` 会结束并重新启动客户端，请在扫码登录后等待命令结束。探测器只报告是否命中与表数量，不显示明文密钥。

默认工作区按来源分开放置，互不混写：

- 个微：`data/wechat/chatlog.db` 与 `data/wechat/keyring.dpapi`，页面端口 8765
- 企微：`data/wecom/chatlog.db` 与 `data/wecom/keyring.dpapi`，页面端口 8766

`init` / `import-live` / `serve` 不带 `--source` 时会同时使用这两套目录。可用 `--source wechat|wecom` 只操作其中一个，或用 `--database` 覆盖路径。

```powershell
uv run chatlog-assistant --source wechat import-live
uv run chatlog-assistant --source wecom serve
```

## 生产采集路径（WxJava 会话存档）

官方会话内容存档由 `collector-wxjava` 拉取、解密、补全通讯录后写入 `data/wecom/inbox/*.jsonl`。Python 只做物流问题分类，不解析本机 `WXWork` 加密库。

```powershell
mvn -f collector-wxjava/pom.xml test
mvn -f collector-wxjava/pom.xml -q spring-boot:run "-Dspring-boot.run.arguments=--collector.fixture-path=samples/wecom_archive_decrypted.jsonl --collector.exit-after-fixture=true --collector.display-map-path=samples/userid_display.json"
uv run chatlog-assistant --source wecom import-archive --inbox
uv run chatlog-assistant --source wecom serve
```

详情见 [collector-wxjava/README.md](collector-wxjava/README.md)。C# `collector/WeComArchive.Collector` 不再作为生产路径。本地 `Documents\WXWork` 回填是独立模块，尚未接入。

## 规范化导入格式

每行一个 JSON 对象：

```json
{"source":"wecom","source_message_id":"m1","conversation_id":"group-1","sent_at":"2026-08-31T09:02:23+08:00","sender_display":"张某 @中技物流","content":"请安排提货并报价","direction":"outbound"}
```

必填字段为 `source`、`source_message_id`、`conversation_id`、`sent_at`、`sender_display` 和 `content`。`direction` 可选。

## MiniMax-M3 语义识别

关键词分类始终可用。配置国内 MiniMax 密钥后，会对「其他物流问题」、带请求语气但没命中规则、以及人设/提示词等消息调用 **MiniMax-M3**。高置信物流类别仍走本地规则。密钥不会随聊天正文上报。

在项目根目录创建 `.env`：

```
MINIMAX_API_KEY=你的密钥
MINIMAX_API_BASE=https://api.minimaxi.com/v1
MINIMAX_MODEL=MiniMax-M3
```

密钥在 [MiniMax 开放平台（国内）](https://platform.minimaxi.com/) 申请。然后重建已导入数据的分类：

```powershell
uv run chatlog-assistant --source wechat analyze --semantic
uv run chatlog-assistant --source wecom analyze --semantic
```

导入时也可加 `--semantic`。没有密钥时不要加该参数，系统会只用关键词。临时关闭语义识别可设 `CHATLAB_SEMANTIC=0`。

启用后会把截断后的消息正文发往 MiniMax 国内接口；聊天密钥仍只保存在本机 DPAPI。

## 企微本地多帐号分析与回填闭环 (`wecom-local`)

除 WxJava 官方会话存档链路外，支持通过 `wecom-local` 子命令实现已授权本地 WXWork 数据库的快照捕获、解密校验、Protobuf 深度解码、主体识别与双层语义分析闭环：

```powershell
# 发现本机企微登录帐号
uv run chatlog-assistant wecom-local discover

# 导入已解密的数据库目录（离线参考包/回填）
uv run chatlog-assistant wecom-local import-offline --db-dir _tmp_wechat_extract/wechat/wxwork_csv/decrypted/ --account-id offline_ref --semantic

# 启动本地可视化仪表盘（端口 8767）
uv run chatlog-assistant wecom-local serve --port 8767

# 导出物流问题记录并自动进行隐私脱敏
uv run chatlog-assistant wecom-local export --format csv --anonymize --output exports/issues.csv
```

## 指定群的信息提取与上下文分析

`wecom-local` 页面默认按名称模糊筛选“中技AI cosplay”；同名群可加账号 ID 和会话 ID。
统计与导出使用相同筛选条件。指定群重建仅替换该群派生分析，不清空其他群的结果。

```powershell
uv run chatlog-assistant wecom-local rebuild --conversation-name "中技AI cosplay"
uv run chatlog-assistant wecom-local query --conversation-name "中技AI cosplay"
# 明确启用外部模型时，才将该群的有限上下文发送给配置的 MiniMax 接口
uv run chatlog-assistant wecom-local rebuild --conversation-name "中技AI cosplay" --semantic
uv run chatlog-assistant wecom-local serve --port 8767
```

分析保留件数（含 PLT）、重量、体积、尺寸、提货地址、目的地、报价、币种和时效原文。
“620隔日达”中的币种为未知，不自动填人民币。规则优先处理明确物流诉求、即时响应和方案；
模糊诉求与一般回复交由 LLM，附带同账号同会话最近最多 12 条、4 小时内的上下文。
模型不能更改主体，输出的业务字段、紧急和客诉风险证据须出现在当前发言原文中；
未知消息 ID、越界引用、非法结果或调用失败均保留规则结果，并返回 `semantic_errors` 计数。

回复按原始引用 ID、引用正文与作者、运单号、精确 @发送者关联；没有这些依据时，只在 4 小时内存在唯一未给出方案的提问时关联。
即时响应的弱关联窗口为 5 分钟；多笔候选时可暂关联最近开放问题，并保留复核标记。明确 ID / 完整引用可跨越 4 小时窗口；报价存在多笔候选时不强行归属。
“马上”不是方案，报价不会自动解决提货安排；`solved` 在页面中表示“已有相关方案”，不代表运输已完成。
`first_response_seconds`、`first_ack_seconds`、`solution_seconds`、`final_solution_seconds`
分别表示首条有效关联回复、明确即时确认、首个方案、最后一个方案耗时。直接报价不补造确认时间。
尚未设定业务 SLA 阈值，因此不擅自标记“超时”。分类条目数与去重提问数分别统计。

也可导入规范化 JSONL：每行提供 `source_message_id`、`conversation_id`、`conversation_name`、
带时区的 `sent_at`、`sender_display` 和 `content`；可选 `sender_id`、`sender_corp_name`、
`content_type`、`reply_to_message_id`。默认主体仍由身份元数据决定，正文 @提及不参与。

```powershell
uv run chatlog-assistant wecom-local import-jsonl your-chat.jsonl --account-id your-account --conversation-name "中技AI cosplay"
```

合并转发的 `forwarded_messages` 可多层嵌套，不再限制单层。每条子消息保留自己的 ID、
发送者、带时区时间和正文；卡片单独保存，子会话彼此隔离。根群筛选会保留名称不同的子群，
原子群名称放在来源信息中。缺少身份或时间的旧格式结构容器会报告缺口，不伪造消息时间。

原始解包器迭代遍历 Protobuf 长度字段，在 `content` 和 `extra_content` 中识别显式 JSON
`forwarded_messages` 和 XML `recordinfo/datalist/dataitem`（含嵌套 `recorditem`）。未知二进制布局、
远程附件和仅有预览的记录不会被当作已展开。旧本地原库的类型 40 实测包含通话记录，
因此不能仅凭 40/49 编号判定合并转发。2026-09-02 已核验目标群真实类型 4 的原生载荷：
重复字段 1 是子消息，节点字段 1/2/11/13/14/101 分别保留作者、时间、名称、企业及正文，
字段 10 是原会话 ID，不能误作原消息 ID。缺失原消息 ID 时使用明确标记的载荷位置键。
原生引用元数据不重复计为消息；嵌套字段继续递归，损坏字段标记为部分展开。

数据库使用可重复执行的增量扩展，新增 `parent_id`、`root_message_id`、`nesting_depth`、
`provenance_json`；旧行采用空来源和深度 0，不因此被标为原库验证通过。旧列、旧查询接口保留，
无删除或重命名迁移。新版写入仍满足旧版列约束；旧版不理解新增证据字段，不应用于重建递归证据。
迁移前应使用 SQLite backup 留存数据库。业务线索新增多组规格、特殊货物和单证字段，缺失值为 null。

采集和离线导入均对 `.db/-wal/-shm` 做重复读取一致性检查，在内存副本中合并已提交 WAL 并执行
`integrity_check`。不一致或解密失败会停止该批入库。`capture-once` 保存快照文件及哈希清单，
`--full` 用于解析器升级后从头重放；按群采集自动从头扫描且不推进账号全局游标。

```powershell
uv run chatlog-assistant wecom-local capture-once --account-id ACCOUNT_ID --full --conversation-name "中技AI cosplay"
uv run chatlog-assistant wecom-local import-offline --db-dir DECRYPTED_DIR --account-id ACCOUNT_ID --full --conversation-name "中技AI cosplay"
uv run chatlog-assistant wecom-local report --conversation-name "中技AI cosplay" --output data/wecom-local/exports/cosplay
uv run chatlog-assistant wecom-local serve --port 8766
```

`/api/wecom/report` 与 `report` 导出提供不截断的全部已导入消息、去重提问轮次、所有关联回复、
路线报价、特殊货物处理率和四种时延。页面保留类别明细；顶层指标按提问计数，以询价类别为主，
已有方案不表示提货或运输完成。来源完整性单独展示，不由单元测试通过、导入成功或闭环率推断。
JSONL 一律标为 `normalized_unverified`，不能自行宣称已获原库认证。
同名群已存在真实数据库来源时，完整报告默认排除未核验材料；仍可按其账号单独查看。
子范围报告用 `ancestors` 附带范围外父级证据，不额外计入当前消息数。页面、报告和 CSV 使用同一分析口径，
展示层递归隐藏凭据与手机号，SQLite 内保留原始证据。

本地分析交付与来源核验保存在 `data/wecom-local/exports/` 和 `data/wecom-local/audits/`，不纳入版本控制。
解析契约、统计口径和验证方法见 `docs/wecom-recursive-analysis.md`；每次数据范围及限制以本地交付报告为准。

`samples/wecom_cosplay_demo.jsonl` 是根据截图制作的脱敏演示数据，不是真实群聊导出。
可使用独立数据库验证，避免与真实采集数据混合：

```powershell
uv run chatlog-assistant wecom-local import-jsonl samples/wecom_cosplay_demo.jsonl --account-id screenshot-demo --analysis-db data/wecom-cosplay-demo/analysis.db
uv run chatlog-assistant wecom-local serve --analysis-db data/wecom-cosplay-demo/analysis.db --port 8770
```

## 当前企微页面功能与交付状态

企微页面默认方向为“空运业务信息分析”。流程 A 使用明确标注的本地演示价卡批次；流程 B 同时提供通过 `--source-analysis-db` 配置的规范化本机来源，以及用于非技术人员讲解的明确标注合成演示数据。两者不会混合或互相替代：真实来源的完整性校验仍会阻断真实场景；合成链路全程显示“非真实”，不能作为真实聊天或正式报价依据。可用 `--no-demo-fixtures` 隐藏合成演示数据。旧揽收页面仍保留在 `/legacy/pickup`，仅作为历史只读入口；旧表和接口不会被空运模型覆盖。

- **空运价卡**：支持 PDF、XLS/XLSX、DOC/DOCX、CSV、JPG/JPEG/PNG 混合批次；流程 A 以 3 条供应商回复展示 8 个可打开／下载的脱敏本地附件，并在发布后生成可内嵌预览和下载 PDF 的最新价卡单。
- **本地群聊**：默认搜索“中技AI cosplay”，同名群必须按账号和会话明确选择；完整性只针对所选快照／时间范围，确认导入绑定预览摘要并幂等写入空运隔离表。目标根消息、嵌套转发或附件存在本机可见缺口时会阻断真实场景；演示者可明确切换到独立的合成数据继续讲解，但真实导入会保留，且页面不会将合成结果标为真实报价。
- **计费与报价**：按 `cargo_component`／`package_group` 组织货物，单件尺寸才乘 CTN，演示规则按 `÷6000` 计算；B11 人工确认 `+ USD 0.20/KG` 后，流程 B 为每张独立询价票生成一张匿名客户报价单和 PDF。内部测算、供应商原始费率和对客销售价三层分开保存。
- **人工与隐私**：未配置空运审核身份时只能解析和预览；外部视觉模型默认关闭，服务仅监听本机，真实聊天／附件不会被自动发送或提交 Git。

使用已有分析库启动当前页面（不要填企微原始数据库）：

```powershell
uv run chatlog-assistant wecom-local serve `
  --analysis-db data/wecom-airfreight-demo/analysis.db `
  --source-analysis-db data/airfreight-local-source/analysis.db `
  --host 127.0.0.1 --port 8880
```

打开 `http://127.0.0.1:8880/` 后选择实际账号和群聊，或选择带“合成演示数据（非真实）”标记的范围完成讲解。`--analysis-db` 是 Demo 隔离库；`--source-analysis-db` 只读发现规范化本机来源、可重复指定。服务启动时加载 Python 和 HTML，更新代码后需重启对应服务，再刷新浏览器；不需要为了换 UI 重新导入或重建分析。

首次查看空运 Demo，可使用独立数据库启动后打开页面，按流程 A／B 的步骤操作。发布价卡或确认报价前，点击“配置演示审核人”，并在冲突表中逐项选择候选；该身份只用于本地演示留痕，不等价于企业登录或审批。流程 B 必须先选择来源和时间范围、只读预览，再明确确认导入；详情见 [空运 Demo 操作说明](docs/wecom-airfreight-demo-runbook.md)。

用独立临时数据库复现合成验收：

```powershell
uv run python scripts/verify_wecom_pickup.py --verify
uv run python scripts/verify_wecom_pickup.py --serve --port 8879
```

`--verify` 会生成合成 Excel 样例，`--serve` 保持临时验收服务运行。输出位于被 Git 忽略的 `outputs/`，不会触及真实数据库。详见 [操作说明](docs/wecom-pricing-operations.md) 和 [本期验收与限制](docs/wecom-pickup-acceptance.md)。

## 隐私边界与安全规范

- 服务默认仅允许本机 `127.0.0.1` 访问。
- 工作数据库、解密缓存与导入文件均严格保留在本机，并通过 `.gitignore` 排除所有数据库文件（`*.db`、`*.sqlite`）、临时提取目录（`_tmp_wechat_extract/`）与凭据配置。
- 手机号（`138****1234`）、身份证号、银行卡号等敏感信息在导出或输出时支持正则掩码脱敏。
- 通过验证的密钥只保存在对应工作区的 DPAPI 文件中（`data/wechat/keyring.dpapi`、`data/wecom/keyring.dpapi`），日志与 JSON 输出严禁包含密钥十六进制或明文证书。
- `encrypted_sources.py` 在缺少已授权提取器或 DPAPI 密钥时拒绝读取正文。
