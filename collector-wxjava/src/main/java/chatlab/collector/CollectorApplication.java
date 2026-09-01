package chatlab.collector;

import chatlab.collector.audit.AuditGateway;
import chatlab.collector.audit.ChatIngestService;
import chatlab.collector.config.CollectorProperties;
import chatlab.collector.media.MediaQueueService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.annotation.Scheduled;

import jakarta.annotation.PreDestroy;
import java.util.Optional;

@SpringBootApplication
@EnableScheduling
public class CollectorApplication implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(CollectorApplication.class);

    private final ChatIngestService ingestService;
    private final MediaQueueService mediaQueueService;
    private final CollectorProperties properties;
    private final Optional<AuditGateway> auditGateway;
    private final ConfigurableApplicationContext context;

    public CollectorApplication(
            ChatIngestService ingestService,
            MediaQueueService mediaQueueService,
            CollectorProperties properties,
            Optional<AuditGateway> auditGateway,
            ConfigurableApplicationContext context
    ) {
        this.ingestService = ingestService;
        this.mediaQueueService = mediaQueueService;
        this.properties = properties;
        this.auditGateway = auditGateway;
        this.context = context;
    }

    public static void main(String[] args) {
        SpringApplication.run(CollectorApplication.class, args);
    }

    @Override
    public void run(String... args) {
        ChatIngestService.IngestStats stats = ingestService.ingestOnce();
        log.info("startup ingest inserted={} lastSeq={}", stats.inserted(), stats.lastSeq());
        if (properties.isMediaEnabled()) {
            mediaQueueService.drain(20);
        }
        if (properties.fixturePath() != null && properties.isExitAfterFixture()) {
            SpringApplication.exit(context, () -> 0);
        }
    }

    @Scheduled(initialDelayString = "${collector.poll-ms:45000}", fixedDelayString = "${collector.poll-ms:45000}")
    public void poll() {
        if (properties.fixturePath() != null || !properties.isLive()) {
            return;
        }
        ingestService.ingestOnce();
        if (properties.isMediaEnabled()) {
            mediaQueueService.drain(20);
        }
    }

    @PreDestroy
    public void shutdownSdks() {
        auditGateway.ifPresent(AuditGateway::closeAllSdks);
    }
}
