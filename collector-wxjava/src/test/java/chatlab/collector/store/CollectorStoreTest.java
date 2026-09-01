package chatlab.collector.store;

import chatlab.collector.mapping.MappedChat;
import chatlab.collector.mapping.MediaRef;
import chatlab.collector.mapping.UnifiedMessage;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.sqlite.SQLiteConfig;
import org.sqlite.SQLiteDataSource;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CollectorStoreTest {

    @TempDir
    Path tempDir;

    @Test
    void messageAndCursorCommitTogetherAndAreIdempotent() {
        CollectorStore store = store();
        MappedChat first = mapped("m1", 10001);
        assertTrue(store.commitMessage("corpA", "{\"msgid\":\"m1\"}", first));
        assertEquals(10001, store.getLastSeq("corpA"));
        assertFalse(store.commitMessage("corpA", "{\"msgid\":\"m1\"}", first));
        assertEquals(10001, store.getLastSeq("corpA"));

        store.commitMessage("corpA", "{\"msgid\":\"m2\"}", mapped("m2", 10002));
        assertEquals(10002, store.getLastSeq("corpA"));
        assertEquals(2, store.pendingMedia(10).size());
    }

    @Test
    void skipStillAdvancesCursor() {
        CollectorStore store = store();
        store.commitSkip("corpA", 9);
        assertEquals(9, store.getLastSeq("corpA"));
    }

    private CollectorStore store() {
        SQLiteConfig config = new SQLiteConfig();
        config.setJournalMode(SQLiteConfig.JournalMode.WAL);
        SQLiteDataSource dataSource = new SQLiteDataSource(config);
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("collector.sqlite"));
        CollectorStore store = new CollectorStore(dataSource, new ObjectMapper());
        store.initialize();
        return store;
    }

    private static MappedChat mapped(String msgId, long seq) {
        UnifiedMessage message = new UnifiedMessage();
        message.setCorpId("corpA");
        message.setSeq(seq);
        message.setMsgId(msgId);
        message.setSenderId("XuJinSheng");
        message.setText("请安排提货");
        return new MappedChat(message, List.of(new MediaRef("file-" + msgId, "image", "")));
    }
}
