# WxJava WeCom Archive Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the C# SKIT sidecar as the production WeCom ingest path with a Windows Java/WxJava collector that pulls, decrypts, enriches, and emits unified JSON for the existing Python logistics analyzer.

**Architecture:** `collector-wxjava` depends on Maven `weixin-java-cp` (not a WxJava fork). It uses only `WxCpMsgAuditService` safe APIs, SQLite (`ingest_cursor` + `raw_message` in one transaction), contact cache, and a media queue. Python stays the analysis layer and consumes unified JSONL from `data/wecom/inbox`. Local `Documents\WXWork` backfill stays a separate future module.

**Tech Stack:** 64-bit JDK 17, Spring Boot 3.3 scheduled app, `weixin-java-cp` `4.8.5-20260818.151216`, official Windows Finance SDK v3 DLLs under `runtime/wecom-sdk/`, Python 3.11 `chatlog-assistant`, SQLite.

## Global Constraints

- Pin WxJava to `4.8.5-20260818.151216` (includes PR `#3848` + ThreadLocal SDK). Never use `LATEST` or floating timestamps.
- Only call `getChatRecords`, `getDecryptChatData` / `getChatRecordPlainText`, `downloadMediaFile`, `closeThreadLocalSdk`, `closeAllSdks`.
- Forbidden: `getChatDatas`, `getDecryptData(sdk, ...)`, `getChatPlainText(sdk, ...)`, `getMediaFile(sdk, ...)`, `Finance.DestroySdk(...)`.
- DLL directory is `D:\chatlab\runtime\wecom-sdk\`, never `C:\Windows\System32`. `msgAuditLibPath` must list dependency DLLs first.
- Use 会话存档 `msgAuditSecret`, not a normal self-built app secret (`48002` otherwise).
- Cursor and `raw_message` commit in the same SQLite transaction.
- Subject: `sender_corp_name` in {中技, 中技物流} or display suffix `@中技` / `@中技物流` → `zhongji`; body `@mentions` never count.
- Do not parse local `WXWork` databases in this collector.
- Do not commit secrets, PEM files, or DLLs. Do not git-commit unless the user asks.
- C# `collector/WeComArchive.Collector` is no longer the production path.

## File Map

- Create: `collector-wxjava/pom.xml`
- Create: `collector-wxjava/src/main/java/chatlab/collector/CollectorApplication.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/config/CollectorProperties.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/config/WxCpCollectorConfig.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/audit/AuditGateway.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/audit/WxJavaAuditGateway.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/audit/ChatIngestService.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/mapping/UnifiedMessage.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/mapping/UnifiedMessageMapper.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/mapping/SubjectRules.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/contact/ContactRecord.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/contact/ContactCacheService.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/media/MediaQueueService.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/store/CollectorStore.java`
- Create: `collector-wxjava/src/main/java/chatlab/collector/export/InboxExporter.java`
- Create: `collector-wxjava/src/main/resources/application.yml`
- Create: `collector-wxjava/src/main/resources/application-local.yml.example`
- Create: `collector-wxjava/src/test/java/chatlab/collector/mapping/SubjectRulesTest.java`
- Create: `collector-wxjava/src/test/java/chatlab/collector/mapping/UnifiedMessageMapperTest.java`
- Create: `collector-wxjava/src/test/java/chatlab/collector/store/CollectorStoreTest.java`
- Create: `collector-wxjava/README.md`
- Create: `runtime/wecom-sdk/README.md`
- Create: `samples/unified_message.jsonl`
- Modify: `src/chatlog_assistant/models.py`, `subject.py`, `sources/wecom_archive.py`, `pipeline.py`, `cli.py`, `workspaces.py`
- Modify: `tests/test_subject.py`, `tests/test_wecom_archive.py`
- Modify: `README.md`, `.gitignore`

---

### Task 1: Java mapper + subject rules (no SDK)

**Files:** SubjectRules, UnifiedMessage, UnifiedMessageMapper + tests

**Interfaces:**
- Consumes: decrypted official chat JSON (`msgid`, `from`, `tolist`, `roomid`, `msgtime`, `msgtype`, nested content)
- Produces: `UnifiedMessageMapper.map(seq, corpId, plaintextJson, ContactRecord) -> Optional<UnifiedMessage>`
- Produces: `SubjectRules.resolve(senderName, senderCorpName, displaySuffix) -> zhongji|other`

- [ ] Unit tests: text, image sdkfileid, mixed, skip switch, body `@中技物流` ignored, corp name 中技物流 → zhongji
- [ ] Implementation with Jackson `SNAKE_CASE`

### Task 2: SQLite store + same-transaction cursor

**Files:** CollectorStore + tests

**Schema:** `ingest_cursor`, `raw_message` (UNIQUE msg_id), `contact_cache`, `media_task`, `analysis_result`

- [ ] Insert message + bump `last_seq` in one connection commit
- [ ] Idempotent `INSERT OR IGNORE` on `msg_id`; cursor still advances
- [ ] Crash-safe: if commit fails, seq is not updated

### Task 3: Spring Boot ingest loop with safe WxJava APIs

**Files:** CollectorApplication, config, AuditGateway, ChatIngestService, InboxExporter

- [ ] Fixture mode reads `samples/wecom_archive_decrypted.jsonl` without native DLL
- [ ] Live mode: `getChatRecords` → `getChatRecordPlainText` → store → JSONL inbox; `finally closeThreadLocalSdk()`; `@PreDestroy closeAllSdks()`
- [ ] `msgAuditLibPath` built from `runtime/wecom-sdk/` with `lib*` DLLs first
- [ ] Secrets from env only: `WECOM_CORP_ID`, `WECOM_MSG_AUDIT_SECRET`, `WECOM_MSG_AUDIT_PRI_KEY` / `WECOM_RSA_PRIVATE_KEY_PATH`

### Task 4: Contacts + Q3 subject

**Files:** ContactCacheService

- Internal userid → user API name + configured `own-corp-name`
- External `wm`/`wo`/`wb` → external contact `name` + `corp_name`
- Cache in `contact_cache`; miss falls back to raw id / subject `other`

### Task 5: Media queue (after plaintext ingest)

**Files:** MediaQueueService

- Enqueue image/voice/video/file/emotion `sdkfileid` as `pending`
- Worker calls `downloadMediaFile` only; retry 5 times; never block plaintext commit
- Successful paths written into unified JSON `media_paths`

### Task 6: Python unified JSON + inbox + analysis

**Files:** Python mapper/subject/pipeline/cli + tests

- Accept unified JSON (`msg_id`, `sender_corp_name`, `text`, …) in `import-archive`
- Subject uses corp name **or** display suffix; body `@` ignored
- `import-archive --inbox` watches `data/wecom/inbox/*.jsonl` and runs existing classifier

### Task 7: Docs and ignore rules

- Document DLL layout, IP allowlist, secret distinction, build order
- `.gitignore`: `runtime/wecom-sdk/*.dll`, `collector-wxjava/target/`, `application-local.yml`
- Explicitly out of scope: `C:\Users\Administrator\Documents\WXWork` history backfill

## Spec coverage

- Windows + JDK 17 + weixin-java-cp 4.8.5-20260818.151216: Task 3 pom
- Safe APIs only / ThreadLocal finally: Task 3
- DLL path + msgAuditSecret: Task 3 + runtime README
- seq → decrypt → SQLite → unified JSON → Python: Tasks 2–6
- ingest_cursor same transaction: Task 2
- Subject corp name + suffix, not body: Tasks 1 + 6
- Media async: Task 5
- WXWork not in live path: Task 7 / Global Constraints
