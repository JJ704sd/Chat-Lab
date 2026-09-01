package chatlab.collector.mapping;

import chatlab.collector.contact.ContactRecord;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class UnifiedMessageMapperTest {

    private UnifiedMessageMapper mapper;

    @BeforeEach
    void setUp() {
        mapper = new UnifiedMessageMapper(new ObjectMapper());
    }

    @Test
    void mapsTextAndZhongjiSubjectFromCorp() throws Exception {
        String json = """
                {"seq":196,"msgid":"m-text","action":"send","from":"XuJinSheng","tolist":["icefog"],"roomid":"","msgtime":1547087894783,"msgtype":"text","text":{"content":"请安排提货并报价"}}
                """;
        MappedChat mapped = mapper.map(196, "corp", json, new ContactRecord("XuJinSheng", "张浩", "中技物流", "internal")).orElseThrow();
        UnifiedMessage message = mapped.message();
        assertEquals("wecom", message.getSource());
        assertEquals("m-text", message.getMsgId());
        assertEquals("dm:XuJinSheng:icefog", message.getConversationId());
        assertEquals("张浩", message.getSenderName());
        assertEquals("中技物流", message.getSenderCorpName());
        assertEquals("zhongji", message.getSubject());
        assertEquals("请安排提货并报价", message.getText());
        assertTrue(mapped.media().isEmpty());
    }

    @Test
    void bodyMentionDoesNotChangeSubject() {
        String json = """
                {"msgid":"m-body","from":"icefog","tolist":["XuJinSheng"],"roomid":"","msgtime":1,"msgtype":"text","text":{"content":"@中技物流 请报价"}}
                """;
        UnifiedMessage message = mapper.map(1, "corp", json, new ContactRecord("icefog", "客服", "嘉航盛物流", "internal")).orElseThrow().message();
        assertEquals("other", message.getSubject());
        assertTrue(message.getText().contains("@中技物流"));
    }

    @Test
    void roomIdWinsConversation() {
        String json = """
                {"msgid":"room-1","from":"XuJinSheng","tolist":["icefog"],"roomid":"wrjc7bDwYAOAhf9quEwRRxyyoMm0QAAA","msgtime":1,"msgtype":"text","text":{"content":"货物延误"}}
                """;
        UnifiedMessage message = mapper.map(2, "corp", json, ContactRecord.unknown("XuJinSheng")).orElseThrow().message();
        assertEquals("wrjc7bDwYAOAhf9quEwRRxyyoMm0QAAA", message.getConversationId());
        assertEquals("wrjc7bDwYAOAhf9quEwRRxyyoMm0QAAA", message.getRoomId());
    }

    @Test
    void imageCollectsSdkFileIdWithoutDownload() {
        String json = """
                {"msgid":"img-1","from":"wmErxtDgAA","tolist":["kenshin"],"roomid":"","msgtime":1,"msgtype":"image","image":{"sdkfileid":"CtYBMzA2","filesize":70961}}
                """;
        MappedChat mapped = mapper.map(3, "corp", json, ContactRecord.unknown("wmErxtDgAA")).orElseThrow();
        assertEquals("image", mapped.message().getMsgType());
        assertTrue(mapped.message().getText().contains("sdkfileid=CtYBMzA2"));
        assertEquals(1, mapped.media().size());
        assertEquals("CtYBMzA2", mapped.media().get(0).sdkFileId());
        assertTrue(mapped.message().getMediaPaths().isEmpty());
    }

    @Test
    void mixedJoinsTextAndImage() {
        String json = """
                {"msgid":"mixed-1","from":"HeMiao","tolist":["HeChangTian"],"roomid":"wr_room","msgtime":1,"msgtype":"mixed","mixed":{"item":[{"type":"text","content":"{\\"content\\":\\"你好\\\\n\\"}"},{"type":"image","content":"{\\"sdkfileid\\":\\"abc\\"}"}]}}
                """;
        MappedChat mapped = mapper.map(4, "corp", json, ContactRecord.unknown("HeMiao")).orElseThrow();
        assertEquals("mixed", mapped.message().getMsgType());
        assertTrue(mapped.message().getText().contains("你好"));
        assertTrue(mapped.message().getText().contains("sdkfileid=abc"));
        assertEquals("abc", mapped.media().get(0).sdkFileId());
    }

    @Test
    void switchIsSkipped() {
        String json = """
                {"msgid":"s1","action":"switch","user":"XuJinSheng","time":1}
                """;
        assertTrue(mapper.map(5, "corp", json, ContactRecord.unknown("XuJinSheng")).isEmpty());
    }
}
