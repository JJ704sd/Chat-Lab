package chatlab.collector.mapping;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.List;

@JsonInclude(JsonInclude.Include.NON_NULL)
public class UnifiedMessage {

    private String source = "wecom";

    @JsonProperty("corp_id")
    private String corpId = "";

    private long seq;

    @JsonProperty("msg_id")
    private String msgId = "";

    @JsonProperty("msg_time")
    private long msgTime;

    @JsonProperty("room_id")
    private String roomId = "";

    @JsonProperty("sender_id")
    private String senderId = "";

    @JsonProperty("sender_name")
    private String senderName = "";

    @JsonProperty("sender_corp_name")
    private String senderCorpName = "";

    private String subject = "other";

    @JsonProperty("msg_type")
    private String msgType = "text";

    private String text = "";

    @JsonProperty("media_paths")
    private List<String> mediaPaths = new ArrayList<>();

    @JsonProperty("conversation_id")
    private String conversationId = "";

    @JsonProperty("raw_json")
    private JsonNode rawJson;

    public String getSource() {
        return source;
    }

    public void setSource(String source) {
        this.source = source;
    }

    public String getCorpId() {
        return corpId;
    }

    public void setCorpId(String corpId) {
        this.corpId = corpId == null ? "" : corpId;
    }

    public long getSeq() {
        return seq;
    }

    public void setSeq(long seq) {
        this.seq = seq;
    }

    public String getMsgId() {
        return msgId;
    }

    public void setMsgId(String msgId) {
        this.msgId = msgId == null ? "" : msgId;
    }

    public long getMsgTime() {
        return msgTime;
    }

    public void setMsgTime(long msgTime) {
        this.msgTime = msgTime;
    }

    public String getRoomId() {
        return roomId;
    }

    public void setRoomId(String roomId) {
        this.roomId = roomId == null ? "" : roomId;
    }

    public String getSenderId() {
        return senderId;
    }

    public void setSenderId(String senderId) {
        this.senderId = senderId == null ? "" : senderId;
    }

    public String getSenderName() {
        return senderName;
    }

    public void setSenderName(String senderName) {
        this.senderName = senderName == null ? "" : senderName;
    }

    public String getSenderCorpName() {
        return senderCorpName;
    }

    public void setSenderCorpName(String senderCorpName) {
        this.senderCorpName = senderCorpName == null ? "" : senderCorpName;
    }

    public String getSubject() {
        return subject;
    }

    public void setSubject(String subject) {
        this.subject = subject == null ? "other" : subject;
    }

    public String getMsgType() {
        return msgType;
    }

    public void setMsgType(String msgType) {
        this.msgType = msgType == null ? "unknown" : msgType;
    }

    public String getText() {
        return text;
    }

    public void setText(String text) {
        this.text = text == null ? "" : text;
    }

    public List<String> getMediaPaths() {
        return mediaPaths;
    }

    public void setMediaPaths(List<String> mediaPaths) {
        this.mediaPaths = mediaPaths == null ? new ArrayList<>() : mediaPaths;
    }

    public String getConversationId() {
        return conversationId;
    }

    public void setConversationId(String conversationId) {
        this.conversationId = conversationId == null ? "" : conversationId;
    }

    public JsonNode getRawJson() {
        return rawJson;
    }

    public void setRawJson(JsonNode rawJson) {
        this.rawJson = rawJson;
    }
}
