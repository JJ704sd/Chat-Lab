# 企微会话存档采集器（WxJava）

Windows 上的 Spring Boot 定时采集服务：用 [WxJava](https://github.com/binarywang/WxJava) `weixin-java-cp` **安全 API** 拉取企业微信会话内容存档，解密后写入 SQLite，再导出统一 JSONL 给现有 Python 物流分析层。

本模块是 Maven 依赖，不复制、不修改 WxJava 仓库。

```text
Windows + Java/WxJava
  拉取 → 解密 → 通讯录补全 → SQLite → data/wecom/inbox/*.jsonl
                ↓
现有 Python chatlog-assistant
  问题分类、回复匹配、解决方案统计
```

## 版本

| 项 | 值 |
| --- | --- |
| JDK | 64 位 17 |
| `weixin-java-cp` | `4.8.5-20260818.151216`（含 PR `#3848` ThreadLocal 生命周期；禁止 `LATEST`） |
| 官方 SDK | 企业微信 Windows 会话存档 SDK v3 |

只允许调用：

- `getChatRecords`
- `getDecryptChatData` / `getChatRecordPlainText`
- `downloadMediaFile`
- `closeThreadLocalSdk`（任务 `finally`）
- `closeAllSdks`（进程退出）

禁止：`getChatDatas`、`getDecryptData(sdk, …)`、`getChatPlainText(sdk, …)`、`getMediaFile(sdk, …)`、`Finance.DestroySdk`。

## DLL

不要拷到 `C:\Windows\System32`。放到：

```text
D:\chatlab\runtime\wecom-sdk\
```

至少包含官方包中的：

- `libcrypto-1_1-x64.dll`
- `libssl-1_1-x64.dll`
- `libcurl-x64.dll`
- `WeWorkFinanceSdk.dll`

采集器会扫描该目录，并把文件名含 `lib` 的 DLL 排在前面。也可显式设置 `WECOM_MSG_AUDIT_LIB_PATH`：

```text
D:/chatlab/runtime/wecom-sdk/libcrypto-1_1-x64.dll,libssl-1_1-x64.dll,libcurl-x64.dll,WeWorkFinanceSdk.dll
```

## 密钥与白名单

会话存档 **专用** Secret，不能用普通自建应用 Secret，否则会出现 `48002 API接口无权限调用`。

环境变量（不要写进 git）：

| 变量 | 含义 |
| --- | --- |
| `WECOM_CORP_ID` | 企业 ID |
| `WECOM_MSG_AUDIT_SECRET` | 会话内容存档 Secret |
| `WECOM_RSA_PRIVATE_KEY_PATH` 或 `WECOM_MSG_AUDIT_PRI_KEY` | 解密私钥 |
| `WECOM_CONTACT_SECRET` | 可选，通讯录/外部联系人 |
| `WECOM_OWN_CORP_NAME` | 本企业对外名称，如 `中技物流` |

企微管理后台需要把本机出口 IP 加入会话存档白名单。

## 命令

本机若访问 Maven Central TLS 失败，可用阿里云镜像：

```powershell
mvn -f collector-wxjava/pom.xml -s collector-wxjava/.mvn/local-settings.xml test
```

无 DLL、无密钥时用夹具跑通 `seq → SQLite → inbox`（不下载媒体）：

```powershell
cd D:\chatlab
mvn -f collector-wxjava/pom.xml test
mvn -f collector-wxjava/pom.xml -q spring-boot:run "-Dspring-boot.run.arguments=--collector.fixture-path=samples/wecom_archive_decrypted.jsonl --collector.exit-after-fixture=true --collector.db-path=data/wecom/collector.sqlite --collector.inbox-dir=data/wecom/inbox --collector.display-map-path=samples/userid_display.json"
```

## 首次真拉取 10 条操作与排错

### 1. 准备工作检查清单

- [ ] **官方 Windows 64位 DLL**：确保以下 4 个文件已放置在 `D:\chatlab\runtime\wecom-sdk\`（不要放 `C:\Windows\System32`）：
  - `libcrypto-1_1-x64.dll`
  - `libssl-1_1-x64.dll`
  - `libcurl-x64.dll`
  - `WeWorkFinanceSdk.dll`
  > 获取方式：登录企业微信管理后台 (`work.weixin.qq.com`) -> **管理工具** -> **会话内容存档** -> 下载 **Windows 64位 SDK** 压缩包获取。
- [ ] **会话存档专属 Secret**：企业微信管理后台「管理工具」->「会话内容存档」专用的 Secret（**切勿**使用自建应用 Secret）。
- [ ] **RSA 私钥文件**：企微后台配置公钥时对应的 RSA 私钥 PEM 文件（如 `D:\chatlab\secrets\msg-audit-private.pem`）。
- [ ] **出口 IP 白名单**：确保已将本机当前出口公网 IP 添加到企业微信管理后台「会话内容存档」的 IP 白名单中。

### 2. 本机执行命令

在 PowerShell 中配置环境变量并启动采集器（拉取 10 条且不开启媒体队列）：

```powershell
cd D:\chatlab

# 配置企微与采集参数（注意不要提交到 Git）
$env:WECOM_CORP_ID="wwxxxxxxxxxxxxxx"
$env:WECOM_MSG_AUDIT_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$env:WECOM_RSA_PRIVATE_KEY_PATH="D:\chatlab\secrets\msg-audit-private.pem"
$env:WECOM_OWN_CORP_NAME="中技物流"

$env:COLLECTOR_LIVE="true"
$env:COLLECTOR_PULL_LIMIT="10"
$env:COLLECTOR_MEDIA_ENABLED="false"

# 若 Maven 不在全局 PATH 中，指定本地 Maven 与 local-settings.xml 运行
$jdk = 'C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot'
$env:JAVA_HOME = $jdk
$env:PATH = "$jdk\bin;$env:TEMP\apache-maven-3.9.9\bin;$env:PATH"

mvn -f collector-wxjava/pom.xml -s collector-wxjava/.mvn/local-settings.xml spring-boot:run
```

如果使用 `cmd.exe`：

```cmd
cd /d D:\chatlab
set WECOM_CORP_ID=wwxxxxxxxxxxxxxx
set WECOM_MSG_AUDIT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set WECOM_RSA_PRIVATE_KEY_PATH=D:\chatlab\secrets\msg-audit-private.pem
set WECOM_OWN_CORP_NAME=中技物流
set COLLECTOR_LIVE=true
set COLLECTOR_PULL_LIMIT=10
set COLLECTOR_MEDIA_ENABLED=false
set JAVA_HOME=C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot
set PATH=C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot\bin;%TEMP%\apache-maven-3.9.9\bin;%PATH%

mvn -f collector-wxjava/pom.xml -s collector-wxjava/.mvn/local-settings.xml spring-boot:run
```

### 3. 验收标准

1. **日志输出**：控制台输出 `startup ingest inserted=... lastSeq=...`，SDK 正常初始化与释放，无 JVM 崩溃、无无效指针与重复 DestroySdk。
2. **SQLite 落库**：`data/wecom/collector.sqlite` 中 `ingest_cursor.last_seq` 正常递增，`raw_message` 表中按 `msg_id` 唯一落库明文。
3. **Inbox 产物**：`data/wecom/inbox/` 目录下生成 `archive-YYYYMMDDTHHMMSS.jsonl`，包含 `source`, `corp_id`, `seq`, `msg_id`, `msg_time`, `room_id`, `sender_id`, `sender_name`, `sender_corp_name`, `subject`, `msg_type`, `text`, `media_paths`, `raw_json` 等字段。
4. **Python 分析层导入**：
   ```powershell
   uv run chatlog-assistant --source wecom import-archive --inbox
   ```
   检查导入条数与中技主体判定（`sender_corp_name` 为「中技」/「中技物流」或展示后缀 `@中技` / `@中技物流` 判定为 `zhongji`，正文 `@` 不参与主体判定）。

### 4. 常见错误定位与修复

| 现象 / 错误码 | 根本原因 | 修复操作 |
| --- | --- | --- |
| **`48002` API接口无权限** | 使用了自建应用 Secret 或通讯录 Secret，而非会话存档专用 Secret | 进入企微管理后台「管理工具」->「会话内容存档」复制专用 Secret，更新 `WECOM_MSG_AUDIT_SECRET` |
| **`UnsatisfiedLinkError` / DLL 加载失败** | 缺少依赖 DLL、32/64位不匹配、或未按依赖顺序加载 | 检查 `runtime/wecom-sdk/` 下是否齐备 4 个 64 位 DLL；确认未将 DLL 拷入 `System32`；代码已自动保证 `lib*.dll` 优先加载 |
| **解密失败 (`DecryptChatData` / `pkcs`)** | 私钥格式不匹配或公私钥版本不一致 | 企微后台默认公钥格式为 PKCS#1（对应系统默认 `COLLECTOR_PKCS=2`）；若导出为 PKCS#8 格式私钥，可设置 `COLLECTOR_PKCS=1`；并检查后台当前生效版本序号与私钥是否对应 |
| **IP 拒绝 / IP not in whitelist** | 采集机当前出口公网 IP 未加白 | 访问 `https://api.ipify.org` 查询本机公网 IP，在企微管理后台会话内容存档白名单中添加该 IP |
| **没有拉到消息 (`records=0`)** | 企微后台配置的开启会话存档员工范围内暂无新消息，或 `seq` 已经是最新 | 发送测试消息，或检查企微后台开启存档的员工名单范围与同意状态 |

## 表结构

SQLite：`data/wecom/collector.sqlite`

- `ingest_cursor`：每个 `corp_id` 的最大 `seq`，与 `raw_message` 同事务提交
- `raw_message`：解密明文，`msg_id` 唯一
- `contact_cache`：成员/外部联系人姓名与企业名
- `media_task`：媒体下载状态与重试
- `analysis_result`：预留给 Python 回写分类结果

本地 `Documents\WXWork` 历史库不走这条链路。
