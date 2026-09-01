package chatlab.collector.audit;

import chatlab.collector.config.CollectorProperties;
import me.chanjar.weixin.cp.api.WxCpMsgAuditService;
import me.chanjar.weixin.cp.bean.msgaudit.WxCpChatDatas;

import java.util.List;

public class WxJavaAuditGateway implements AuditGateway {

    private final WxCpMsgAuditService msgAuditService;
    private final CollectorProperties properties;

    public WxJavaAuditGateway(WxCpMsgAuditService msgAuditService, CollectorProperties properties) {
        this.msgAuditService = msgAuditService;
        this.properties = properties;
    }

    @Override
    public List<WxCpChatDatas.WxCpChatData> getChatRecords(long seq, long limit) throws Exception {
        return msgAuditService.getChatRecords(seq, limit, null, null, properties.getTimeoutMs());
    }

    @Override
    public String getChatRecordPlainText(WxCpChatDatas.WxCpChatData chatData) throws Exception {
        return msgAuditService.getChatRecordPlainText(chatData, properties.getPkcs());
    }

    @Override
    public void downloadMediaFile(String sdkFileId, String targetFilePath) throws Exception {
        msgAuditService.downloadMediaFile(
                sdkFileId, null, null, properties.getMediaTimeoutMs(), targetFilePath);
    }

    @Override
    public void closeThreadLocalSdk() {
        msgAuditService.closeThreadLocalSdk();
    }

    @Override
    public void closeAllSdks() {
        msgAuditService.closeAllSdks();
    }
}
