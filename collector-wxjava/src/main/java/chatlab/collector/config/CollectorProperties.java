package chatlab.collector.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.nio.file.Path;

@ConfigurationProperties(prefix = "collector")
public class CollectorProperties {

    private String corpId = "";
    private String msgAuditSecret = "";
    private String msgAuditPriKey = "";
    private String rsaPrivateKeyPath = "";
    private String contactSecret = "";
    private String ownCorpName = "";
    private String msgAuditLibPath = "";
    private String sdkDir = "runtime/wecom-sdk";
    private String dbPath = "data/wecom/collector.sqlite";
    private String inboxDir = "data/wecom/inbox";
    private String mediaDir = "data/wecom/media";
    private String displayMapPath = "data/wecom/userid_display.json";
    private String fixturePath = "";
    private boolean exitAfterFixture;
    private boolean live;
    private int pullLimit = 10;
    private long pollMs = 45_000L;
    private long timeoutMs = 10_000L;
    private long mediaTimeoutMs = 30_000L;
    private int pkcs = 2;
    private boolean mediaEnabled;
    private int mediaMaxRetries = 5;

    public Path dbPath() {
        return Path.of(dbPath);
    }

    public Path inboxDir() {
        return Path.of(inboxDir);
    }

    public Path mediaDir() {
        return Path.of(mediaDir);
    }

    public Path sdkDir() {
        return Path.of(sdkDir);
    }

    public Path displayMapPath() {
        return Path.of(displayMapPath);
    }

    public Path fixturePath() {
        return fixturePath == null || fixturePath.isBlank() ? null : Path.of(fixturePath);
    }

    public Path rsaPrivateKeyPath() {
        return rsaPrivateKeyPath == null || rsaPrivateKeyPath.isBlank() ? null : Path.of(rsaPrivateKeyPath);
    }

    public String getCorpId() {
        return corpId;
    }

    public void setCorpId(String corpId) {
        this.corpId = corpId;
    }

    public String getMsgAuditSecret() {
        return msgAuditSecret;
    }

    public void setMsgAuditSecret(String msgAuditSecret) {
        this.msgAuditSecret = msgAuditSecret;
    }

    public String getMsgAuditPriKey() {
        return msgAuditPriKey;
    }

    public void setMsgAuditPriKey(String msgAuditPriKey) {
        this.msgAuditPriKey = msgAuditPriKey;
    }

    public String getRsaPrivateKeyPath() {
        return rsaPrivateKeyPath;
    }

    public void setRsaPrivateKeyPath(String rsaPrivateKeyPath) {
        this.rsaPrivateKeyPath = rsaPrivateKeyPath;
    }

    public String getContactSecret() {
        return contactSecret;
    }

    public void setContactSecret(String contactSecret) {
        this.contactSecret = contactSecret;
    }

    public String getOwnCorpName() {
        return ownCorpName;
    }

    public void setOwnCorpName(String ownCorpName) {
        this.ownCorpName = ownCorpName;
    }

    public String getMsgAuditLibPath() {
        return msgAuditLibPath;
    }

    public void setMsgAuditLibPath(String msgAuditLibPath) {
        this.msgAuditLibPath = msgAuditLibPath;
    }

    public String getSdkDir() {
        return sdkDir;
    }

    public void setSdkDir(String sdkDir) {
        this.sdkDir = sdkDir;
    }

    public String getDbPath() {
        return dbPath;
    }

    public void setDbPath(String dbPath) {
        this.dbPath = dbPath;
    }

    public String getInboxDir() {
        return inboxDir;
    }

    public void setInboxDir(String inboxDir) {
        this.inboxDir = inboxDir;
    }

    public String getMediaDir() {
        return mediaDir;
    }

    public void setMediaDir(String mediaDir) {
        this.mediaDir = mediaDir;
    }

    public String getDisplayMapPath() {
        return displayMapPath;
    }

    public void setDisplayMapPath(String displayMapPath) {
        this.displayMapPath = displayMapPath;
    }

    public String getFixturePath() {
        return fixturePath;
    }

    public void setFixturePath(String fixturePath) {
        this.fixturePath = fixturePath;
    }

    public boolean isExitAfterFixture() {
        return exitAfterFixture;
    }

    public void setExitAfterFixture(boolean exitAfterFixture) {
        this.exitAfterFixture = exitAfterFixture;
    }

    public boolean isLive() {
        return live;
    }

    public void setLive(boolean live) {
        this.live = live;
    }

    public int getPullLimit() {
        return pullLimit;
    }

    public void setPullLimit(int pullLimit) {
        this.pullLimit = pullLimit;
    }

    public long getPollMs() {
        return pollMs;
    }

    public void setPollMs(long pollMs) {
        this.pollMs = pollMs;
    }

    public long getTimeoutMs() {
        return timeoutMs;
    }

    public void setTimeoutMs(long timeoutMs) {
        this.timeoutMs = timeoutMs;
    }

    public long getMediaTimeoutMs() {
        return mediaTimeoutMs;
    }

    public void setMediaTimeoutMs(long mediaTimeoutMs) {
        this.mediaTimeoutMs = mediaTimeoutMs;
    }

    public int getPkcs() {
        return pkcs;
    }

    public void setPkcs(int pkcs) {
        this.pkcs = pkcs;
    }

    public boolean isMediaEnabled() {
        return mediaEnabled;
    }

    public void setMediaEnabled(boolean mediaEnabled) {
        this.mediaEnabled = mediaEnabled;
    }

    public int getMediaMaxRetries() {
        return mediaMaxRetries;
    }

    public void setMediaMaxRetries(int mediaMaxRetries) {
        this.mediaMaxRetries = mediaMaxRetries;
    }
}
