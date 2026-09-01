package chatlab.collector.store;

import chatlab.collector.contact.ContactRecord;
import chatlab.collector.mapping.MappedChat;
import chatlab.collector.mapping.MediaRef;
import chatlab.collector.mapping.UnifiedMessage;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

public class CollectorStore {

    private static final String SCHEMA = """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS ingest_cursor (
                corp_id TEXT PRIMARY KEY,
                last_seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw_message (
                msg_id TEXT PRIMARY KEY,
                corp_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                msg_time INTEGER,
                room_id TEXT,
                sender_id TEXT,
                plaintext TEXT NOT NULL,
                unified_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_message_seq ON raw_message(corp_id, seq);
            CREATE TABLE IF NOT EXISTS contact_cache (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                corp_name TEXT,
                user_type TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_task (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT NOT NULL,
                sdk_file_id TEXT NOT NULL,
                msg_type TEXT,
                local_path TEXT,
                status TEXT NOT NULL,
                retries INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(msg_id, sdk_file_id)
            );
            CREATE TABLE IF NOT EXISTS analysis_result (
                msg_id TEXT PRIMARY KEY,
                category TEXT,
                response_kind TEXT,
                is_solution INTEGER,
                payload_json TEXT,
                updated_at TEXT NOT NULL
            );
            """;

    private final DataSource dataSource;
    private final ObjectMapper objectMapper;

    public CollectorStore(DataSource dataSource, ObjectMapper objectMapper) {
        this.dataSource = dataSource;
        this.objectMapper = objectMapper;
    }

    public void initialize() {
        try (Connection connection = dataSource.getConnection(); Statement statement = connection.createStatement()) {
            for (String part : SCHEMA.split(";")) {
                String sql = part.strip();
                if (!sql.isBlank()) {
                    statement.execute(sql);
                }
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to initialize collector sqlite", ex);
        }
    }

    public long getLastSeq(String corpId) {
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement(
                     "SELECT last_seq FROM ingest_cursor WHERE corp_id = ?")) {
            statement.setString(1, corpId);
            try (ResultSet rows = statement.executeQuery()) {
                return rows.next() ? rows.getLong(1) : 0L;
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to read ingest_cursor", ex);
        }
    }

    /**
     * Persist plaintext + unified JSON, enqueue media, and advance seq in one commit.
     *
     * @return true when a new {@code msg_id} was inserted
     */
    public void commitSkip(String corpId, long seq) {
        if (seq <= 0) {
            return;
        }
        String now = Instant.now().toString();
        try (Connection connection = dataSource.getConnection()) {
            boolean previousAutoCommit = connection.getAutoCommit();
            connection.setAutoCommit(false);
            try {
                upsertCursor(connection, corpId, seq, now);
                connection.commit();
            } catch (Exception ex) {
                connection.rollback();
                throw ex;
            } finally {
                connection.setAutoCommit(previousAutoCommit);
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to advance ingest_cursor", ex);
        }
    }

    public boolean commitMessage(String corpId, String plaintext, MappedChat mapped) {
        UnifiedMessage message = mapped.message();
        String unifiedJson = writeJson(message);
        String now = Instant.now().toString();
        try (Connection connection = dataSource.getConnection()) {
            boolean previousAutoCommit = connection.getAutoCommit();
            connection.setAutoCommit(false);
            try {
                boolean inserted = insertRaw(connection, corpId, plaintext, message, unifiedJson, now);
                enqueueMedia(connection, mapped.media(), message.getMsgId(), now);
                upsertCursor(connection, corpId, message.getSeq(), now);
                connection.commit();
                return inserted;
            } catch (Exception ex) {
                connection.rollback();
                throw ex;
            } finally {
                connection.setAutoCommit(previousAutoCommit);
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to commit raw_message + ingest_cursor", ex);
        }
    }

    public void upsertContact(ContactRecord contact) {
        String now = Instant.now().toString();
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement("""
                     INSERT INTO contact_cache(user_id, name, corp_name, user_type, updated_at)
                     VALUES (?, ?, ?, ?, ?)
                     ON CONFLICT(user_id) DO UPDATE SET
                         name=excluded.name,
                         corp_name=excluded.corp_name,
                         user_type=excluded.user_type,
                         updated_at=excluded.updated_at
                     """)) {
            statement.setString(1, contact.userId());
            statement.setString(2, contact.name());
            statement.setString(3, contact.corpName());
            statement.setString(4, contact.userType());
            statement.setString(5, now);
            statement.executeUpdate();
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to upsert contact_cache", ex);
        }
    }

    public Optional<ContactRecord> findContact(String userId) {
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement(
                     "SELECT user_id, name, corp_name, user_type FROM contact_cache WHERE user_id = ?")) {
            statement.setString(1, userId);
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next()) {
                    return Optional.empty();
                }
                return Optional.of(new ContactRecord(
                        rows.getString(1),
                        rows.getString(2),
                        rows.getString(3),
                        rows.getString(4)));
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to read contact_cache", ex);
        }
    }

    public List<MediaTask> pendingMedia(int limit) {
        List<MediaTask> tasks = new ArrayList<>();
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement("""
                     SELECT id, msg_id, sdk_file_id, msg_type, retries
                     FROM media_task
                     WHERE status IN ('pending', 'retry')
                     ORDER BY id
                     LIMIT ?
                     """)) {
            statement.setInt(1, Math.max(1, limit));
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    tasks.add(new MediaTask(
                            rows.getLong(1),
                            rows.getString(2),
                            rows.getString(3),
                            rows.getString(4),
                            rows.getInt(5)));
                }
            }
            return tasks;
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to list media_task", ex);
        }
    }

    public void markMedia(long id, String status, String localPath, String error, int retries) {
        String now = Instant.now().toString();
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement("""
                     UPDATE media_task
                     SET status=?, local_path=?, error=?, retries=?, updated_at=?
                     WHERE id=?
                     """)) {
            statement.setString(1, status);
            statement.setString(2, localPath);
            statement.setString(3, error);
            statement.setInt(4, retries);
            statement.setString(5, now);
            statement.setLong(6, id);
            statement.executeUpdate();
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to update media_task", ex);
        }
    }

    public void attachMediaPath(String msgId, String localPath) {
        try (Connection connection = dataSource.getConnection();
             PreparedStatement select = connection.prepareStatement(
                     "SELECT unified_json FROM raw_message WHERE msg_id = ?")) {
            select.setString(1, msgId);
            String json;
            try (ResultSet rows = select.executeQuery()) {
                if (!rows.next()) {
                    return;
                }
                json = rows.getString(1);
            }
            UnifiedMessage message = objectMapper.readValue(json, UnifiedMessage.class);
            if (!message.getMediaPaths().contains(localPath)) {
                message.getMediaPaths().add(localPath);
            }
            try (PreparedStatement update = connection.prepareStatement(
                    "UPDATE raw_message SET unified_json = ? WHERE msg_id = ?")) {
                update.setString(1, writeJson(message));
                update.setString(2, msgId);
                update.executeUpdate();
            }
        } catch (Exception ex) {
            throw new IllegalStateException("failed to attach media path", ex);
        }
    }

    public Optional<String> unifiedJson(String msgId) {
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement(
                     "SELECT unified_json FROM raw_message WHERE msg_id = ?")) {
            statement.setString(1, msgId);
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next()) {
                    return Optional.empty();
                }
                return Optional.ofNullable(rows.getString(1));
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("failed to read unified_json", ex);
        }
    }

    private boolean insertRaw(
            Connection connection,
            String corpId,
            String plaintext,
            UnifiedMessage message,
            String unifiedJson,
            String now
    ) throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement("""
                INSERT OR IGNORE INTO raw_message(
                    msg_id, corp_id, seq, msg_time, room_id, sender_id, plaintext, unified_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """)) {
            statement.setString(1, message.getMsgId());
            statement.setString(2, corpId);
            statement.setLong(3, message.getSeq());
            statement.setLong(4, message.getMsgTime());
            statement.setString(5, message.getRoomId());
            statement.setString(6, message.getSenderId());
            statement.setString(7, plaintext);
            statement.setString(8, unifiedJson);
            statement.setString(9, now);
            return statement.executeUpdate() == 1;
        }
    }

    private void enqueueMedia(Connection connection, List<MediaRef> media, String msgId, String now) throws SQLException {
        if (media == null || media.isEmpty()) {
            return;
        }
        try (PreparedStatement statement = connection.prepareStatement("""
                INSERT OR IGNORE INTO media_task(
                    msg_id, sdk_file_id, msg_type, status, retries, updated_at
                ) VALUES (?, ?, ?, 'pending', 0, ?)
                """)) {
            for (MediaRef ref : media) {
                statement.setString(1, msgId);
                statement.setString(2, ref.sdkFileId());
                statement.setString(3, ref.msgType());
                statement.setString(4, now);
                statement.addBatch();
            }
            statement.executeBatch();
        }
    }

    private void upsertCursor(Connection connection, String corpId, long seq, String now) throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement("""
                INSERT INTO ingest_cursor(corp_id, last_seq, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(corp_id) DO UPDATE SET
                    last_seq=MAX(ingest_cursor.last_seq, excluded.last_seq),
                    updated_at=excluded.updated_at
                """)) {
            statement.setString(1, corpId);
            statement.setLong(2, seq);
            statement.setString(3, now);
            statement.executeUpdate();
        }
    }

    private String writeJson(UnifiedMessage message) {
        try {
            return objectMapper.writeValueAsString(message);
        } catch (JsonProcessingException ex) {
            throw new IllegalStateException("failed to serialize unified message", ex);
        }
    }

    public record MediaTask(long id, String msgId, String sdkFileId, String msgType, int retries) {
    }
}
