# WeCom Official Finance Archive Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land a Windows production path that pulls WeCom session-archive messages via the official Finance SDK, writes normalized JSONL, and feeds the existing Python analysis workspace without decrypting local `WXWork` databases.

**Architecture:** A C# sidecar wraps `WeWorkFinanceSdk.dll` through SKIT, persists `seq` / raw payloads / media jobs, and drops JSONL into `data/wecom/inbox`. Python maps that JSONL into `data/wecom/chatlog.db` and keeps MiniMax / subject / issue analysis unchanged. Local `Documents\WXWork` probing stays a separate, non-production backfill module.

**Tech Stack:** .NET 8, `SKIT.FlurlHttpClient.Wechat.Work` ≥ 3.2.1, official Windows Finance SDK v3 (`WeWorkFinanceSdk.dll` + OpenSSL/curl x64), Python 3.11 `chatlog-assistant`, SQLite.

## Global Constraints

- Do not treat local wxSQLite3 / debugger key capture as the production ingest path.
- Do not commit unless the user explicitly asks.
- Never log RSA private keys, Corp Secret, or key hex.
- Official archive cannot unbounded-rewind history; poll every 30–60 seconds and persist max `seq`.
- Media must download asynchronously with retries; analysis must not block on SDK file bytes.
- Subject rule stays: only sender display suffix; `@中技` / `@中技物流` → `zhongji`; body `@mentions` do not count.
- Local `C:\Users\Administrator\Documents\WXWork` backfill must not share code paths with the archive collector.
- PyWxDump and WeChatMsg are not dependencies.
- Java/WxJava is an optional second sidecar that must emit the same JSONL contract; do not rewrite Python analysis into JVM.

## File Map

- Create: `src/chatlog_assistant/sources/wecom_archive.py` — official decrypted JSON → `Message`
- Create: `tests/test_wecom_archive.py`
- Create: `samples/wecom_archive_decrypted.jsonl`
- Create: `samples/userid_display.json`
- Modify: `src/chatlog_assistant/pipeline.py` — `import_archive_jsonl`
- Modify: `src/chatlog_assistant/cli.py` — `import-archive`
- Modify: `src/chatlog_assistant/workspaces.py` — inbox / collector paths
- Create: `collector/WeComArchive.Collector/WeComArchive.Collector.csproj`
- Create: `collector/WeComArchive.Collector/Program.cs`
- Create: `collector/WeComArchive.Collector/Mapping/ArchiveMapper.cs`
- Create: `collector/WeComArchive.Collector/Storage/CollectorDb.cs`
- Create: `collector/WeComArchive.Collector/appsettings.json`
- Create: `collector/WeComArchive.Collector.Tests/ArchiveMapperTests.cs`
- Modify: `README.md`, `.gitignore`

---

### Task 1: Official archive JSON → Message

**Files:**
- Create: `src/chatlog_assistant/sources/wecom_archive.py`
- Create: `tests/test_wecom_archive.py`
- Create: `samples/wecom_archive_decrypted.jsonl`
- Create: `samples/userid_display.json`

**Interfaces:**
- Consumes: official decrypted chat object (`msgid`, `from`, `tolist`, `roomid`, `msgtime`, `msgtype`, nested content)
- Produces: `message_from_archive(item, display_map) -> Message | None`; `conversation_id_for(...) -> str`; `content_from_archive(item) -> tuple[str, str]`

- [ ] **Step 1: Write failing tests** for text, image, mixed, 1:1 vs room, external msgid, subject suffix from display map, body `@` ignored.

- [ ] **Step 2: Implement mapper**

```python
SOURCE = "wecom"
CN_TZ = timezone(timedelta(hours=8))

def conversation_id_for(roomid: str | None, from_user: str | None, to_list: list[str] | None) -> str:
    room = (roomid or "").strip()
    if room:
        return room
    people = [item for item in [from_user, *(to_list or [])] if item]
    return "dm:" + ":".join(sorted(people))
```

- [ ] **Step 3: Run** `python -m unittest tests.test_wecom_archive -q` Expected: PASS

---

### Task 2: `import-archive` CLI

**Files:**
- Modify: `src/chatlog_assistant/pipeline.py`
- Modify: `src/chatlog_assistant/cli.py`
- Modify: `src/chatlog_assistant/workspaces.py`

**Interfaces:**
- Consumes: `message_from_archive`, existing `import_messages`
- Produces: `import_archive_jsonl(storage, path, display_map=None, semantic=None) -> int`; CLI `import-archive`

A line is treated as already-normalized JSONL if it has `source` and `source_message_id`; otherwise it is official archive JSON.

Default workspace is `--source wecom`. Cursor key `archive-inbox:{filename}` records imported size/mtime so reruns are idempotent with `UNIQUE(source, source_message_id)`.

- [ ] **Step 1: Test** dual-format file import into a temp wecom DB.
- [ ] **Step 2: Wire CLI** `import-archive PATH [--display-map PATH] [--semantic]`
- [ ] **Step 3: Run unittest +** `python -m chatlog_assistant --source wecom import-archive samples/wecom_archive_decrypted.jsonl`

---

### Task 3: C# collector store + JSONL emit (no native DLL required)

**Files:**
- Create collector project listed above
- Test: `collector/WeComArchive.Collector.Tests/ArchiveMapperTests.cs`

**Interfaces:**
- Consumes: decrypted JSON (fixture or SKIT `DecryptChatRecordResponse`)
- Produces: JSONL objects with `source`, `source_message_id`, `conversation_id`, `sent_at`, `sender_display`, `content`, `direction`, `content_type`, `raw_ref`

SQLite (`data/wecom/collector.sqlite`):

```sql
CREATE TABLE seq_cursors (
    corp_id TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE messages (
    msgid TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    jsonl_json TEXT NOT NULL,
    ingested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE media_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid TEXT NOT NULL,
    sdk_file_id TEXT NOT NULL,
    status TEXT NOT NULL,
    retries INTEGER NOT NULL DEFAULT 0,
    local_path TEXT,
    error TEXT,
    UNIQUE(msgid, sdk_file_id)
);
```

`dotnet run -- --fixture samples/wecom_archive_decrypted.jsonl` writes `data/wecom/inbox/*.jsonl` and advances a fixture cursor.

- [ ] **Step 1: Mapper tests in C# match Python fixtures**
- [ ] **Step 2: CollectorDb + Program fixture mode**
- [ ] **Step 3:** `dotnet test` Expected: PASS

---

### Task 4: Live SKIT Finance pull

**Files:**
- Modify: `collector/WeComArchive.Collector/Program.cs`
- Create: `collector/WeComArchive.Collector/Finance/FinanceSession.cs`
- Modify: `collector/WeComArchive.Collector/appsettings.json`

**Interfaces:**
- Consumes: `WECOM_CORP_ID`, `WECOM_CHAT_SECRET`, `WECOM_RSA_PRIVATE_KEY_PATH`, optional `WECOM_RSA_KEY_VERSION` (default 1)
- Produces: loop `GetChatRecords(lastSeq, limit=1000)` → decrypt each row → upsert messages → persist max seq → copy JSONL to inbox

Native files (not in git): `WeWorkFinanceSdk.dll`, `libcrypto-1_1-x64.dll`, `libcurl-x64.dll`, `libssl-1_1-x64.dll` next to the published exe or `collector/native/`.

Singleton `WechatWorkFinanceClient`; `Dispose` on shutdown. Poll interval 45s. Limit 1000 per call until a page returns fewer rows.

Contacts: load `samples/userid_display.json` or `data/wecom/userid_display.json`. Missing userid uses raw `from` (subject falls to `other`). Later optional Task: Work contacts API.

- [ ] **Step 1: FinanceSession using SKIT types from Basic_FinanceSDK.md**
- [ ] **Step 2: Fail closed if DLL or secret missing; do not crash Python**
- [ ] **Step 3: Manual run only when the admin console archive is enabled**

---

### Task 5: Async media tasks

**Files:**
- Create: `collector/WeComArchive.Collector/Media/MediaDownloader.cs`

**Interfaces:**
- Consumes: `sdkfileid` from image/voice/video/file/emotion/mixed
- Produces: files under `data/wecom/media/{msgid}/...`; JSONL `content` becomes `[image path=...]` after success, `[image pending sdkfileid=...]` before

Retry 5 times with exponential backoff. Never wait for media before writing the text JSONL row.

- [ ] **Step 1: Queue media_tasks on ingest**
- [ ] **Step 2: Background worker calls `ExecuteGetMediaFileAsync`**
- [ ] **Step 3: Test retry/failure status without hitting the network (fake file id)**

---

### Task 6: Inbox watch + README

**Files:**
- Modify: `src/chatlog_assistant/cli.py` — `import-archive --inbox --interval`
- Modify: `README.md`, `.gitignore`

Watch `data/wecom/inbox/*.jsonl` every 45s, import, move to `data/wecom/inbox/done/`. Document admin prerequisites: 会话内容存档, CorpID, Secret, RSA, IP allowlist. State that archive does not read `WXWork` on disk.

- [ ] **Step 1: Inbox importer uses `import_cursors`**
- [ ] **Step 2: README production path vs local backfill**

---

### Task 7 (optional): WxJava sidecar

Same JSONL contract. Only start if SKIT/Windows DLL load is blocked. Do not replace Python analysis.

---

## Spec coverage

- Official Windows SDK + C# sidecar + existing Python analysis: Tasks 1–6
- SKIT Finance doc / v3 DLL: Task 4
- `seq_cursors + messages + media_tasks`: Task 3–5
- Subject `@中技` / `@中技物流`: Task 1 display map + existing `SubjectResolver`
- Poll 30–60s: Task 4 default 45s
- Media async: Task 5
- Local WXWork decoupled: Global Constraints + Task 6 README
- Java allowed later: Task 7
- PyWxDump / WeChatMsg not used: Global Constraints

## Admin blockers (cannot be coded around)

Official pull stays `success=false` until the WeCom admin console has 会话内容存档 enabled, CorpID/Secret, RSA keypair, and the machine IP allowlisted. The collector must still build and fixture-test without those secrets.
