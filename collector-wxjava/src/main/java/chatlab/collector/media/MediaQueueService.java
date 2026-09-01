package chatlab.collector.media;

import chatlab.collector.audit.AuditGateway;
import chatlab.collector.config.CollectorProperties;
import chatlab.collector.store.CollectorStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HexFormat;
import java.security.MessageDigest;
import java.util.List;
import java.util.Optional;

public class MediaQueueService {

    private static final Logger log = LoggerFactory.getLogger(MediaQueueService.class);

    private final CollectorStore store;
    private final CollectorProperties properties;
    private final Optional<AuditGateway> auditGateway;

    public MediaQueueService(
            CollectorStore store,
            CollectorProperties properties,
            Optional<AuditGateway> auditGateway
    ) {
        this.store = store;
        this.properties = properties;
        this.auditGateway = auditGateway;
    }

    public int drain(int limit) {
        if (!properties.isMediaEnabled() || auditGateway.isEmpty()) {
            return 0;
        }
        AuditGateway gateway = auditGateway.get();
        List<CollectorStore.MediaTask> tasks = store.pendingMedia(limit);
        int done = 0;
        try {
            for (CollectorStore.MediaTask task : tasks) {
                int retries = task.retries();
                try {
                    Path target = targetPath(task);
                    Files.createDirectories(target.getParent());
                    gateway.downloadMediaFile(task.sdkFileId(), target.toAbsolutePath().toString());
                    store.markMedia(task.id(), "done", target.toString(), null, retries);
                    store.attachMediaPath(task.msgId(), target.toString());
                    done += 1;
                } catch (Exception ex) {
                    retries += 1;
                    String status = retries >= properties.getMediaMaxRetries() ? "failed" : "retry";
                    store.markMedia(task.id(), status, null, safeError(ex), retries);
                    log.warn("media download {} for msgid hash retries={}", status, retries);
                }
            }
        } finally {
            gateway.closeThreadLocalSdk();
        }
        return done;
    }

    private Path targetPath(CollectorStore.MediaTask task) {
        String digest = sha256(task.sdkFileId()).substring(0, 16);
        String ext = extensionFor(task.msgType());
        return properties.mediaDir().resolve(task.msgId()).resolve(digest + ext);
    }

    private static String extensionFor(String msgType) {
        return switch (msgType == null ? "" : msgType) {
            case "image" -> ".jpg";
            case "voice" -> ".amr";
            case "video" -> ".mp4";
            case "emotion" -> ".gif";
            case "file" -> ".bin";
            default -> ".bin";
        };
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (Exception ex) {
            return Integer.toHexString(value.hashCode());
        }
    }

    private static String safeError(Exception ex) {
        String message = ex.getMessage();
        if (message == null) {
            return ex.getClass().getSimpleName();
        }
        return message.replaceAll("(?i)(secret|key|token)=\\S+", "$1=***");
    }
}
