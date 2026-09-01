package chatlab.collector.audit;

import chatlab.collector.config.CollectorProperties;
import chatlab.collector.contact.ContactCacheService;
import chatlab.collector.export.InboxExporter;
import chatlab.collector.mapping.MappedChat;
import chatlab.collector.mapping.UnifiedMessage;
import chatlab.collector.mapping.UnifiedMessageMapper;
import chatlab.collector.store.CollectorStore;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import me.chanjar.weixin.cp.bean.msgaudit.WxCpChatDatas;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

public class ChatIngestService {

    private static final Logger log = LoggerFactory.getLogger(ChatIngestService.class);

    private final CollectorStore store;
    private final CollectorProperties properties;
    private final UnifiedMessageMapper mapper;
    private final ContactCacheService contacts;
    private final InboxExporter exporter;
    private final ObjectMapper objectMapper;
    private final Optional<AuditGateway> auditGateway;

    public ChatIngestService(
            CollectorStore store,
            CollectorProperties properties,
            UnifiedMessageMapper mapper,
            ContactCacheService contacts,
            InboxExporter exporter,
            ObjectMapper objectMapper,
            Optional<AuditGateway> auditGateway
    ) {
        this.store = store;
        this.properties = properties;
        this.mapper = mapper;
        this.contacts = contacts;
        this.exporter = exporter;
        this.objectMapper = objectMapper;
        this.auditGateway = auditGateway;
    }

    public IngestStats ingestOnce() {
        Path fixture = properties.fixturePath();
        if (fixture != null && !Files.isRegularFile(fixture)) {
            Path fromRepo = Path.of("").toAbsolutePath().resolve(fixture);
            if (Files.isRegularFile(fromRepo)) {
                fixture = fromRepo;
            } else {
                Path fromParent = Path.of("").toAbsolutePath().getParent();
                if (fromParent != null && Files.isRegularFile(fromParent.resolve(fixture))) {
                    fixture = fromParent.resolve(fixture);
                }
            }
        }
        if (fixture != null) {
            return ingestFixture(fixture);
        }
        if (!properties.isLive() || auditGateway.isEmpty()) {
            log.info("collector idle: set COLLECTOR_FIXTURE or collector.live=true with msgAuditSecret");
            return IngestStats.empty();
        }
        return ingestLive();
    }

    public IngestStats ingestFixture(Path path) {
        if (!Files.isRegularFile(path)) {
            throw new IllegalStateException("fixture not found: " + path.toAbsolutePath());
        }
        String corpId = blankTo(properties.getCorpId(), "fixture");
        List<UnifiedMessage> exported = new ArrayList<>();
        int inserted = 0;
        try {
            List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
            for (String line : lines) {
                if (line == null || line.isBlank()) {
                    continue;
                }
                JsonNode root = objectMapper.readTree(line);
                long seq = root.path("seq").asLong(0);
                String from = root.path("from").asText("");
                Optional<MappedChat> mapped = mapper.map(seq, corpId, root, contacts.resolve(from));
                if (mapped.isEmpty()) {
                    if (seq > 0) {
                        store.commitSkip(corpId, seq);
                    }
                    continue;
                }
                if (store.commitMessage(corpId, line, mapped.get())) {
                    inserted += 1;
                    exported.add(mapped.get().message());
                }
            }
        } catch (Exception ex) {
            throw new IllegalStateException("fixture ingest failed", ex);
        }
        Path out = exporter.export(properties.inboxDir(), exported);
        log.info("fixture ingest inserted={} exported={} seq={}", inserted, exported.size(), store.getLastSeq(corpId));
        return new IngestStats(inserted, exported.size(), store.getLastSeq(corpId), out);
    }

    private IngestStats ingestLive() {
        AuditGateway gateway = auditGateway.get();
        String corpId = properties.getCorpId();
        if (corpId == null || corpId.isBlank()) {
            throw new IllegalStateException("WECOM_CORP_ID / collector.corp-id is required for live pull");
        }
        long seq = store.getLastSeq(corpId);
        int limit = Math.min(Math.max(properties.getPullLimit(), 1), 1000);
        List<UnifiedMessage> exported = new ArrayList<>();
        int inserted = 0;
        try {
            List<WxCpChatDatas.WxCpChatData> records = gateway.getChatRecords(seq, limit);
            if (records == null || records.isEmpty()) {
                return new IngestStats(0, 0, seq, null);
            }
            for (WxCpChatDatas.WxCpChatData record : records) {
                long recordSeq = record.getSeq() == null ? 0L : record.getSeq();
                String plaintext;
                try {
                    plaintext = gateway.getChatRecordPlainText(record);
                } catch (Exception ex) {
                    log.warn("decrypt failed seq={} msgid={}; cursor not advanced past this row",
                            recordSeq, record.getMsgId());
                    break;
                }
                JsonNode root = objectMapper.readTree(plaintext);
                String from = root.path("from").asText("");
                Optional<MappedChat> mapped = mapper.map(recordSeq, corpId, root, contacts.resolve(from));
                if (mapped.isEmpty()) {
                    store.commitSkip(corpId, recordSeq);
                    continue;
                }
                if (store.commitMessage(corpId, plaintext, mapped.get())) {
                    inserted += 1;
                    exported.add(mapped.get().message());
                }
            }
        } catch (Exception ex) {
            throw new IllegalStateException("live ingest failed", ex);
        } finally {
            gateway.closeThreadLocalSdk();
        }
        Path out = exporter.export(properties.inboxDir(), exported);
        log.info("live ingest inserted={} exported={} seq={}", inserted, exported.size(), store.getLastSeq(corpId));
        return new IngestStats(inserted, exported.size(), store.getLastSeq(corpId), out);
    }

    private static String blankTo(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }

    public record IngestStats(int inserted, int exported, long lastSeq, Path inboxFile) {
        static IngestStats empty() {
            return new IngestStats(0, 0, 0, null);
        }
    }
}
