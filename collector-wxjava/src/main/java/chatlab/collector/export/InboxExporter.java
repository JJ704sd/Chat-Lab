package chatlab.collector.export;

import chatlab.collector.mapping.UnifiedMessage;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.List;

public class InboxExporter {

    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss")
            .withZone(ZoneOffset.UTC);

    private final ObjectMapper objectMapper;

    public InboxExporter(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public Path export(Path inboxDir, List<UnifiedMessage> messages) {
        if (messages == null || messages.isEmpty()) {
            return null;
        }
        try {
            Files.createDirectories(inboxDir);
            Path out = inboxDir.resolve("archive-" + STAMP.format(Instant.now()) + ".jsonl");
            StringBuilder body = new StringBuilder();
            for (UnifiedMessage message : messages) {
                body.append(objectMapper.writeValueAsString(message)).append('\n');
            }
            Files.writeString(out, body.toString(), StandardCharsets.UTF_8);
            return out;
        } catch (IOException ex) {
            throw new IllegalStateException("failed to write inbox jsonl", ex);
        }
    }
}
