package chatlab.collector.contact;

import chatlab.collector.config.CollectorProperties;
import chatlab.collector.store.CollectorStore;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.file.Files;
import java.util.Locale;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class ContactCacheService {

    private static final Logger log = LoggerFactory.getLogger(ContactCacheService.class);
    private static final Pattern DISPLAY = Pattern.compile("^(?<name>.*?)(?:\\s+@(?<corp>[^@\\s]+))?$");
    private static final String[] EXTERNAL_PREFIXES = {"wm", "wo", "wb"};

    private final CollectorStore store;
    private final CollectorProperties properties;
    private final ObjectMapper objectMapper;
    private final Optional<LiveContactClient> liveClient;

    public ContactCacheService(
            CollectorStore store,
            CollectorProperties properties,
            ObjectMapper objectMapper,
            Optional<LiveContactClient> liveClient
    ) {
        this.store = store;
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.liveClient = liveClient;
        loadDisplayMap();
    }

    public ContactRecord resolve(String userId) {
        if (userId == null || userId.isBlank()) {
            return ContactRecord.unknown("");
        }
        Optional<ContactRecord> cached = store.findContact(userId);
        if (cached.isPresent()) {
            return cached.get();
        }
        ContactRecord fetched = fetch(userId);
        store.upsertContact(fetched);
        return fetched;
    }

    public void putDisplay(String userId, String display) {
        ContactRecord parsed = parseDisplay(userId, display);
        store.upsertContact(parsed);
    }

    private ContactRecord fetch(String userId) {
        if (liveClient.isPresent()) {
            try {
                Optional<ContactRecord> remote = liveClient.get().lookup(userId, isExternal(userId));
                if (remote.isPresent()) {
                    return remote.get();
                }
            } catch (Exception ex) {
                log.warn("contact lookup failed for userid prefix={}, using fallback", prefix(userId));
            }
        }
        if (!isExternal(userId) && properties.getOwnCorpName() != null && !properties.getOwnCorpName().isBlank()) {
            return new ContactRecord(userId, userId, properties.getOwnCorpName(), "internal");
        }
        return ContactRecord.unknown(userId);
    }

    private void loadDisplayMap() {
        var path = properties.displayMapPath();
        if (!Files.isRegularFile(path)) {
            path = java.nio.file.Path.of("samples/userid_display.json");
        }
        if (!Files.isRegularFile(path)) {
            return;
        }
        try {
            JsonNode root = objectMapper.readTree(path.toFile());
            if (!root.isObject()) {
                return;
            }
            root.fields().forEachRemaining(entry -> putDisplay(entry.getKey(), entry.getValue().asText("")));
        } catch (Exception ex) {
            log.warn("failed to load userid display map from {}", path);
        }
    }

    static ContactRecord parseDisplay(String userId, String display) {
        if (display == null || display.isBlank()) {
            return ContactRecord.unknown(userId);
        }
        Matcher matcher = DISPLAY.matcher(display.strip());
        if (!matcher.matches()) {
            return new ContactRecord(userId, display.strip(), "", isExternal(userId) ? "external" : "internal");
        }
        String name = matcher.group("name") == null || matcher.group("name").isBlank()
                ? userId
                : matcher.group("name").strip();
        String corp = matcher.group("corp") == null ? "" : matcher.group("corp");
        return new ContactRecord(userId, name, corp, isExternal(userId) ? "external" : "internal");
    }

    public static boolean isExternal(String userId) {
        if (userId == null) {
            return false;
        }
        String folded = userId.toLowerCase(Locale.ROOT);
        for (String prefix : EXTERNAL_PREFIXES) {
            if (folded.startsWith(prefix)) {
                return true;
            }
        }
        return false;
    }

    private static String prefix(String userId) {
        return userId.length() <= 2 ? userId : userId.substring(0, 2);
    }

    public interface LiveContactClient {
        Optional<ContactRecord> lookup(String userId, boolean external) throws Exception;
    }
}
