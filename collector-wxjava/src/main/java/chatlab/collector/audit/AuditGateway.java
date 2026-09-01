package chatlab.collector.audit;

import me.chanjar.weixin.cp.bean.msgaudit.WxCpChatDatas;

import java.util.List;

/**
 * Narrow gateway over {@link me.chanjar.weixin.cp.api.WxCpMsgAuditService} safe APIs only.
 */
public interface AuditGateway {

    List<WxCpChatDatas.WxCpChatData> getChatRecords(long seq, long limit) throws Exception;

    String getChatRecordPlainText(WxCpChatDatas.WxCpChatData chatData) throws Exception;

    void downloadMediaFile(String sdkFileId, String targetFilePath) throws Exception;

    void closeThreadLocalSdk();

    void closeAllSdks();
}
