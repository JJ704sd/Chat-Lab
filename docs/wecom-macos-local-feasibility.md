# Mac 企业微信本地采集可行性验证

首次验证：2026-09-05 12:17；适配后复验：12:36（Asia/Shanghai）

## 结论

后续范围调整：用户已暂停重签客户端/运行时取钥方案，并授权通过企业微信界面取得真实演示样本。该样本已用于 [四阶段 Demo](management-demo.md)。本报告中的“0 条”仅指从 Mac 原消息库解密导出的记录；界面样本不作为原库采集已实现的证据。

当前 ZJ-Chatlab 的 Windows 本地采集链路不能直接用于这台 Mac。本轮已加入 Mac 目录发现和原生 AES 运算，并验证原消息库与 WAL 可以取得一致性快照；尚未取得并验证数据库密钥，未解密原消息库，未导出目标群最近一天的完整消息。

这证明的是“现有实现尚不兼容、当前普通用户读取路径受阻”，不证明所有 Mac 本地采集方法都不可行。数据库页结构与已有 wxSQLite3 识别规则相符，只能作为复用候选，不能代替实际解密与表结构验证。

本报告的采集证据全部来自本地文件读取、项目函数调用和系统进程属性查询。企业微信界面、截图、复制聊天、UI 自动化不作为采集路径或验收证据。

## 本机环境与目标

| 项目 | 实测值 |
| --- | --- |
| 系统 | macOS 26.6.2，arm64 |
| 企业微信 | 5.0.10，构建 99949 |
| 应用标识 | com.tencent.WeWorkMac |
| 目标群 | 广州佳联迅&深圳中技 航线沟通群 |
| 预期范围 | 最近有消息的一天；须读取原库后确认最新消息日期与时间范围 |
| 原生消息库 | `~/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles/<profile>/Messages1/Info.db` |
| 原生会话库 | 同目录 `Session.db` |
| 当前采集结果 | 0 条通过本地原库解密导出的消息 |

## 修改前的本机实测

| 验证项 | 结果 | 能证明什么 |
| --- | --- | --- |
| 原生目录与文件发现 | 已找到 `Info.db`、`Session.db` 及三个搜索索引库 | Mac 本地消息相关文件存在 |
| 目标群定位 | `conv_snapshot` 中群名精确命中；本地向量索引中有 79 个同名片段索引项 | 目标群存在于本地元数据；79 不是消息条数 |
| 普通 SQLite 读取 | 五个消息相关数据库均无明文 SQLite 文件头，直接读取 schema 失败 | 无法直接以普通 SQLite 打开原库 |
| 页格式识别 | 五个库均通过已有 `has_wxsqlite3_header_shape`，页大小 4096；WAL 头有效 | 已有页处理逻辑可能有复用空间；真实解密尚未验证 |
| 一致性快照 | `Session.db` 与 `Info.db` 均通过项目现有双读校验，重试 0 次 | 文件快照这一步可以在 Mac 工作；不代表逻辑内容完整或解密成功 |
| 无密钥解码 | 现有解码器返回 `Missing or invalid 16-byte raw key` | 尚未具备可验证的实际密钥；不是某个已取得密钥被拒绝 |
| Windows 账号发现函数 | 对 Mac `Profiles` 目录返回 0 个账号 | 现有数字账号目录及 `Data/message.db` 假设不兼容 Mac |
| 加密运算后端 | 实际调用返回 `Windows CNG AES is only available on Windows` | 当前加密后端必须适配跨平台实现 |
| 密钥保存后端 | 实际调用返回 `DPAPI is only available on Windows` | 当前密钥保存方式不能直接在 Mac 使用 |
| 运行中客户端保护 | 内核查询显示 Hardened Runtime 开启，`get-task-allow` 未开启 | 当前运行进程具备调试保护 |
| 进程访问句柄 | 在工作区沙箱之外，以当前用户调用 `task_for_pid` 返回 5，未取得 task port | 本次普通用户进程访问路径没有打通；没有读取内存、暂停进程或注入代码 |

`Session.db` 快照约 2.0 MB，关联 WAL 约 18.5 MB；`Info.db` 快照约 65.8 MB，关联 WAL 约 20.8 MB。不能只复制主 DB 就声称取得最新数据。快照哈希、完整测试状态与时间戳保存在本地验证结果中。

本地 `ai_chunks_embedding.db` 的已检查主表包含会话、消息编号、时间、片段及向量信息，没有原始消息正文字段。它用于存在性佐证，不能作为完整聊天导出的替代来源。该索引采用 immutable 只读方式检查，未将其视为实时完整快照。

## Windows 与 Mac 的适配边界

| 部分 | 当前判断 | 下一步 |
| --- | --- | --- |
| Web 演示、报价草稿、规范化分析库 | 上一轮已在 Mac 打开并运行页面；不等于采集已适配 | 保持采集层与演示层分开验收 |
| 原库定位与账号识别 | 本轮已增加 Mac Profiles 发现；实机识别 3 个 profile | profile 目录标识不等于已验证的账号身份 |
| DB/WAL 快照读取 | Mac 本机实测可运行 | 继续保留范围绑定与一致性验证 |
| 密钥获取 | 当前关键阻断项 | 先验证可重复、本地、无界面操作的有效密钥获取路径 |
| 密钥保存 | Windows DPAPI 专用 | 获取路径成立后适配 macOS Keychain；Keychain 本身不会自动提供企业微信的数据库密钥 |
| AES 运算与页解密 | 本轮加入 CommonCrypto；标准向量与自建多页库测试通过 | 真实 Mac 页解密仍须有效密钥及完整性校验 |
| 表结构与消息解码 | Mac 原库尚未解密 | 读取真实 schema 后适配，不能假定 `Info.db` 等同 Windows `message.db` |
| 附件定位、日期筛选、增量采集 | 尚未进入验证 | 成功解密之后逐项验证 |

加密后端替换或目录调整无法单独解决密钥获取问题。也不能把“Mac 能打开 Demo”写成“Mac 原库采集已支持”。本轮仅修改项目代码、测试和报告，没有修改客户端签名、系统保护或企业微信文件。

## 本轮实现与复验

- `sources/crypto_native.py`：Windows 继续使用现有 CNG；Mac 使用系统 CommonCrypto 的 AES-CBC 无填充运算。`wxsqlite3.py` 通过这个入口调用，未改变既有页算法。
- `sources/wecom_macos.py` 与 `wecom_paths.py`：识别 Mac 的 32 位十六进制 profile 目录及 `Messages1` 库，返回数据库与 WAL 状态；明确输出 `capture_ready: false` 和未验证原因。
- `wecom_pipeline.py` 与 `wecom_cli.py`：Mac `capture-once` 返回 `macos_capture_not_ready`；`watch` 遇到该阻断立即退出，避免持续输出没有意义的零消息结果。阻断发生在创建分析库、快照文件或访问 DPAPI 前。
- `scripts/probe_wecom_macos.py`：可重复运行的只读检查工具。默认不访问进程；可选 `--pid` 仅检测 task port 是否可取得，不读内存或暂停进程。

实机 `discover` 已识别 3 个 Mac profile。对当前 profile 执行 `capture-once` 的退出码为 1、导入 0 条消息，指定输出目录未创建。重新读取 Info/Session 与 WAL 的快照均通过一致性检查。

执行 `tests.test_wecom_macos`、`tests.test_live_sources`、`tests.test_wecom_local`：40 项测试，37 项通过、3 项 Windows 专用测试跳过。测试包括 NIST AES-CBC 预期密文、合成多页 SQLite 加解密后 `integrity_check` 与正文一致、错误密钥拒绝，以及 Mac 采集阻断前不写分析库。**合成库测试只证明加密后端及既有算法的往返正确，不能证明真实 Mac 企业微信库已解密。** 本轮没有 Windows 实机回归结果。

## 磁盘密钥线索验证

静态读取客户端可找到 `DbKeyManager`、`QueryUserDBKeys`、`GetAllLocalEncryptKey`、`GetPcLocalEncryptKeyRsp` 等字符串及类型信息，也存在 `GetPcLocalEncryptKeyRsp` 与网络请求模板的关联。它们说明有内部密钥管理流程，不能证明任何一个接口可供外部本地采集调用。没有调用内部桥接接口或发出取钥网络请求。

当前 profile 的 `io_data.json/login_keys` 是 Base64 编码的二进制结构，按 Protobuf 字段结构读取后发现两个直接的 16 字节候选字段。仅在内存中将它们分别传给现有 wxSQLite3 首页校验器，测试 `Info.db`、`Session.db`、`Config.db`，六次均未通过。字段值未输出或保存。这个结果只排除了“这两个字段可直接作为现有算法原始密钥”的假设，不能排除其他派生方式、算法或密钥来源。

没有将群名缓存、向量索引项、客户端界面记录或合成消息导入为真实采集数据。

## 后续可能需要的隔离实验与风险

目前仍可继续做静态分析，但没有已验证的纯磁盘取钥方案。运行时验证是另一条待试路径，**不能承诺重新签名就一定能取得密钥**。建议先在独立 macOS 测试账号或备用 Mac 上验证以下步骤：

1. 准备同版本客户端副本、授权测试登录环境和只读数据库快照；不与日常账号共用正在写入的资料目录。测试登录可能影响现有登录状态，需安排测试窗口。
2. 仅对测试副本设置调试所需签名与权限，保留原始安装包。先确认测试副本能启动和登录；不以关闭 SIP 等系统保护作为默认前提，遇到要求扩大权限的情况即停下。
3. 在已允许调试的测试副本中验证候选密钥来源。调试可能短暂停顿或造成测试副本退出；仅校验必要候选字段，不保存全量内存转储，不输出密钥。
4. 用候选密钥离线验证原库与 WAL、schema 和完整性。通过后再适配 Mac 消息字段和目标群日期范围。
5. 退出并移除测试副本及临时敏感材料，保留不含密钥、正文的验证结果。若使用备用机器或独立系统账号，恢复过程不会需要替换日常客户端。

重新签名可能破坏客户端完整性校验、Keychain/容器权限及更新行为；调试步骤还依赖版本和系统策略。即使实验通过，也需把这些部署、升级维护和恢复成本列入立项技术风险，不能直接承诺普遍适配所有 Mac。本轮未执行以上实验。

## 下一阶段验收门槛

按以下顺序推进，前一项未通过时不宣称后续能力完成：

1. 取得与本机实际数据库对应的有效密钥，以数据库页、schema 可读及 `integrity_check` 验证，不以格式相似判定成功。
2. 在解密后的原始会话表中唯一定位目标群，按已验证的原消息时间字段选取最近一天。
3. 保留原消息 ID、发送者身份、时间、文本、引用与附件关联；未知字段和缺失媒体如实记录。
4. 复核一天的范围、消息数量、起止时间、嵌套记录和附件可得性；再次采集验证去重与新增消息。
5. 提前脱敏并生成本地演示快照，接入已对齐的约 5 分钟四阶段演示。

只有这些条件成立后，才将 Mac 本地采集纳入已验证技术方案。当前应标注为“Mac 原库采集验证中；密钥获取未打通”。不以 UI 自动化、模拟聊天或其他采集渠道替代这项验收。

## 复查材料

- 可复用只读验证脚本：`scripts/probe_wecom_macos.py`
- 修改前本机结果：`data/mac-local-validation/result.json`
- 修改后本机结果：`data/mac-local-validation/result-after-adaptation.json`
- 磁盘候选校验结果：`data/mac-local-validation/disk-key-candidates.json`
- 结果位于 Git 忽略的本地数据目录；不保存消息正文、实际账号标识或密钥。旧脚本与旧结果保留用于对照。
- 验证脚本只读本地文件；可选 `--pid` 仅验证进程访问句柄，不读取进程内存。

执行时使用项目兼容的 Python，传入目标 profile、本地群名及输出文件。`--pid` 应通过当前进程列表取得，不沿用旧 PID；脚本还会校验 PID 是否属于企业微信主程序。

```sh
python3 scripts/probe_wecom_macos.py \
  --profile "$HOME/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles/<profile>" \
  --group '广州佳联迅&深圳中技 航线沟通群' \
  --output data/mac-local-validation/result-after-adaptation.json
```

## 系统限制依据

Apple 官方说明：调试工具 entitlement 不意味着能取得所有第三方进程的 task port；目标进程未开启 `get-task-allow` 时仍受系统保护。本机 `csops` 与 `task_for_pid` 结果与这一限制一致，具体失败结论以本机实测为准。

- [Apple：Debugging tool entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.debugger)
- [Apple：CommonCrypto CCCrypt API](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/CCCryptorFinal.3cc.html)
- 项目核查位置：`sources/wecom_pipeline.py`、`sources/wecom_paths.py`、`sources/crypto_win.py`、`secrets.py`、`sources/wxsqlite3.py`、`sources/wecom_decrypter.py`。
