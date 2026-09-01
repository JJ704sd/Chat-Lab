package chatlab.collector.config;

import chatlab.collector.audit.AuditGateway;
import chatlab.collector.audit.ChatIngestService;
import chatlab.collector.audit.WxJavaAuditGateway;
import chatlab.collector.contact.ContactCacheService;
import chatlab.collector.contact.WxCpLiveContactClient;
import chatlab.collector.export.InboxExporter;
import chatlab.collector.mapping.UnifiedMessageMapper;
import chatlab.collector.media.MediaQueueService;
import chatlab.collector.store.CollectorStore;
import com.fasterxml.jackson.databind.ObjectMapper;
import me.chanjar.weixin.cp.api.WxCpService;
import me.chanjar.weixin.cp.api.impl.WxCpServiceImpl;
import me.chanjar.weixin.cp.config.impl.WxCpDefaultConfigImpl;
import org.sqlite.SQLiteConfig;
import org.sqlite.SQLiteDataSource;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import javax.sql.DataSource;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Optional;

@Configuration
@EnableConfigurationProperties(CollectorProperties.class)
public class WxCpCollectorConfig {

    @Bean
    public DataSource dataSource(CollectorProperties properties) {
        Path path = properties.dbPath();
        try {
            Files.createDirectories(path.getParent());
        } catch (Exception ex) {
            throw new IllegalStateException("cannot create sqlite parent dir " + path.getParent(), ex);
        }
        SQLiteConfig sqliteConfig = new SQLiteConfig();
        sqliteConfig.setJournalMode(SQLiteConfig.JournalMode.WAL);
        sqliteConfig.enforceForeignKeys(true);
        SQLiteDataSource dataSource = new SQLiteDataSource(sqliteConfig);
        dataSource.setUrl("jdbc:sqlite:" + path.toAbsolutePath());
        return dataSource;
    }

    @Bean
    public CollectorStore collectorStore(DataSource dataSource, ObjectMapper objectMapper) {
        CollectorStore store = new CollectorStore(dataSource, objectMapper);
        store.initialize();
        return store;
    }

    @Bean
    public UnifiedMessageMapper unifiedMessageMapper(ObjectMapper objectMapper) {
        return new UnifiedMessageMapper(objectMapper);
    }

    @Bean
    public InboxExporter inboxExporter(ObjectMapper objectMapper) {
        return new InboxExporter(objectMapper);
    }

    @Bean
    @ConditionalOnProperty(prefix = "collector", name = "live", havingValue = "true")
    public WxCpService wxCpService(CollectorProperties properties) {
        if (properties.getCorpId() == null || properties.getCorpId().isBlank()) {
            throw new IllegalStateException("collector.corp-id / WECOM_CORP_ID is required");
        }
        String secret = properties.getMsgAuditSecret();
        if (secret == null || secret.isBlank()) {
            throw new IllegalStateException("WECOM_MSG_AUDIT_SECRET is required; do not use a normal app secret");
        }
        String priKey = readPriKey(properties);
        if (priKey.isBlank()) {
            throw new IllegalStateException("WECOM_MSG_AUDIT_PRI_KEY or WECOM_RSA_PRIVATE_KEY_PATH is required");
        }
        String libPath = SdkLibPath.resolve(properties.getMsgAuditLibPath(), properties.sdkDir());
        if (libPath.isBlank()) {
            throw new IllegalStateException("place official Windows SDK DLLs in " + properties.sdkDir().toAbsolutePath()
                    + " or set WECOM_MSG_AUDIT_LIB_PATH");
        }
        WxCpDefaultConfigImpl config = new WxCpDefaultConfigImpl();
        config.setCorpId(properties.getCorpId());
        config.setMsgAuditSecret(secret);
        config.setMsgAuditPriKey(priKey);
        config.setMsgAuditLibPath(libPath);
        if (properties.getContactSecret() != null && !properties.getContactSecret().isBlank()) {
            config.setContactSecret(properties.getContactSecret());
            config.setCorpSecret(properties.getContactSecret());
        } else {
            // Access token APIs still need a secret; session archive Init() prefers msgAuditSecret.
            config.setCorpSecret(secret);
        }
        WxCpServiceImpl service = new WxCpServiceImpl();
        service.setWxCpConfigStorage(config);
        return service;
    }

    @Bean
    @ConditionalOnProperty(prefix = "collector", name = "live", havingValue = "true")
    public AuditGateway auditGateway(WxCpService wxCpService, CollectorProperties properties) {
        return new WxJavaAuditGateway(wxCpService.getMsgAuditService(), properties);
    }

    @Bean
    @ConditionalOnProperty(prefix = "collector", name = "live", havingValue = "true")
    public ContactCacheService.LiveContactClient liveContactClient(
            WxCpService wxCpService,
            CollectorProperties properties
    ) {
        return new WxCpLiveContactClient(wxCpService, properties);
    }

    @Bean
    public ContactCacheService contactCacheService(
            CollectorStore store,
            CollectorProperties properties,
            ObjectMapper objectMapper,
            Optional<ContactCacheService.LiveContactClient> liveClient
    ) {
        return new ContactCacheService(store, properties, objectMapper, liveClient);
    }

    @Bean
    public ChatIngestService chatIngestService(
            CollectorStore store,
            CollectorProperties properties,
            UnifiedMessageMapper mapper,
            ContactCacheService contacts,
            InboxExporter exporter,
            ObjectMapper objectMapper,
            Optional<AuditGateway> auditGateway
    ) {
        return new ChatIngestService(store, properties, mapper, contacts, exporter, objectMapper, auditGateway);
    }

    @Bean
    public MediaQueueService mediaQueueService(
            CollectorStore store,
            CollectorProperties properties,
            Optional<AuditGateway> auditGateway
    ) {
        return new MediaQueueService(store, properties, auditGateway);
    }

    static String readPriKey(CollectorProperties properties) {
        if (properties.getMsgAuditPriKey() != null && !properties.getMsgAuditPriKey().isBlank()) {
            return properties.getMsgAuditPriKey().replace("\\n", "\n");
        }
        Path path = properties.rsaPrivateKeyPath();
        if (path == null) {
            return "";
        }
        try {
            return Files.readString(path, StandardCharsets.UTF_8);
        } catch (Exception ex) {
            throw new IllegalStateException("failed to read RSA private key file", ex);
        }
    }
}
