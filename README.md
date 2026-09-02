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
uv run chatlog-assistant wecom-local import --db-dir _tmp_wechat_extract/wechat/wxwork_csv/decrypted/ --account offline_ref --semantic

# 启动本地可视化仪表盘（端口 8767）
uv run chatlog-assistant wecom-local serve --port 8767

# 导出物流问题记录并自动进行隐私脱敏
uv run chatlog-assistant wecom-local export --format csv --mask --output exports/issues.csv
```

## 隐私边界与安全规范

- 服务默认仅允许本机 `127.0.0.1` 访问。
- 工作数据库、解密缓存与导入文件均严格保留在本机，并通过 `.gitignore` 排除所有数据库文件（`*.db`、`*.sqlite`）、临时提取目录（`_tmp_wechat_extract/`）与凭据配置。
- 手机号（`138****1234`）、身份证号、银行卡号等敏感信息在导出或输出时支持正则掩码脱敏。
- 通过验证的密钥只保存在对应工作区的 DPAPI 文件中（`data/wechat/keyring.dpapi`、`data/wecom/keyring.dpapi`），日志与 JSON 输出严禁包含密钥十六进制或明文证书。
- `encrypted_sources.py` 在缺少已授权提取器或 DPAPI 密钥时拒绝读取正文。
