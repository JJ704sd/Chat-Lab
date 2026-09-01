package chatlab.collector.mapping;

import chatlab.collector.contact.ContactRecord;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.TreeSet;

public class UnifiedMessageMapper {

    private final ObjectMapper mapper;

    public UnifiedMessageMapper(ObjectMapper mapper) {
        this.mapper = mapper;
    }

    public Optional<MappedChat> map(long seq, String corpId, String plaintextJson, ContactRecord contact) {
        try {
            JsonNode root = mapper.readTree(plaintextJson);
            return map(seq, corpId, root, contact);
        } catch (Exception ex) {
            throw new IllegalArgumentException("invalid decrypted chat json", ex);
        }
    }

    public Optional<MappedChat> map(long seq, String corpId, JsonNode root, ContactRecord contact) {
        if (root == null || root.isMissingNode()) {
            return Optional.empty();
        }
        String action = text(root, "action", "send");
        String msgType = text(root, "msgtype", "unknown");
        if ("switch".equals(action) || "switch".equals(msgType)) {
            return Optional.empty();
        }
        String msgId = firstNonBlank(text(root, "msgid", ""), text(root, "msg_id", ""));
        if (msgId.isBlank()) {
            throw new IllegalArgumentException("archive message missing msgid");
        }
        String senderId = firstNonBlank(text(root, "from", ""), text(root, "user", ""));
        ContactRecord resolved = contact == null ? ContactRecord.unknown(senderId) : contact;
        String senderName = firstNonBlank(resolved.name(), senderId, "unknown");
        String senderCorp = resolved.corpName() == null ? "" : resolved.corpName();

        UnifiedMessage message = new UnifiedMessage();
        message.setSource("wecom");
        message.setCorpId(corpId);
        message.setSeq(seq > 0 ? seq : root.path("seq").asLong(0));
        message.setMsgId(msgId);
        message.setMsgTime(readMsgTime(root));
        message.setRoomId(text(root, "roomid", ""));
        message.setSenderId(senderId);
        message.setSenderName(senderName);
        message.setSenderCorpName(senderCorp);
        message.setSubject(SubjectRules.resolve(senderName, senderCorp));
        message.setMsgType(msgType);
        message.setText(contentFrom(root, msgType));
        message.setConversationId(conversationId(message.getRoomId(), senderId, stringList(root.get("tolist"))));
        message.setRawJson(root);

        return Optional.of(new MappedChat(message, mediaRefs(root, msgType)));
    }

    public static String conversationId(String roomId, String fromUser, List<String> toList) {
        String room = roomId == null ? "" : roomId.strip();
        if (!room.isBlank()) {
            return room;
        }
        TreeSet<String> people = new TreeSet<>();
        if (fromUser != null && !fromUser.isBlank()) {
            people.add(fromUser);
        }
        if (toList != null) {
            for (String item : toList) {
                if (item != null && !item.isBlank()) {
                    people.add(item);
                }
            }
        }
        return people.isEmpty() ? "dm:unknown" : "dm:" + String.join(":", people);
    }

    String contentFrom(JsonNode root, String msgType) {
        return switch (msgType) {
            case "text" -> text(root.path("text"), "content", "");
            case "markdown" -> text(root.path("info"), "content", "");
            case "image" -> mediaPlaceholder("image", root.path("image"));
            case "file" -> {
                JsonNode file = root.path("file");
                String name = firstNonBlank(text(file, "filename", ""), "file");
                yield ("[file " + name + " " + mediaId(file) + "]").strip();
            }
            case "voice" -> mediaPlaceholder("voice", root.path("voice"));
            case "video" -> mediaPlaceholder("video", root.path("video"));
            case "emotion" -> mediaPlaceholder("emotion", root.path("emotion"));
            case "link" -> {
                JsonNode link = root.path("link");
                yield (text(link, "title", "") + " " + text(link, "link_url", "")).strip();
            }
            case "location" -> firstNonBlank(
                    text(root.path("location"), "address", ""),
                    text(root.path("location"), "title", ""),
                    "[location]");
            case "revoke" -> ("[revoke " + text(root.path("revoke"), "pre_msgid", "") + "]").strip();
            case "mixed" -> joinMixed(root.path("mixed"));
            default -> {
                JsonNode nested = root.get(msgType);
                if (nested != null && nested.isObject()) {
                    yield nested.toString();
                }
                yield "";
            }
        };
    }

    List<MediaRef> mediaRefs(JsonNode root, String msgType) {
        List<MediaRef> refs = new ArrayList<>();
        collectMedia(root.get(msgType), msgType, refs);
        if ("mixed".equals(msgType)) {
            JsonNode items = root.path("mixed").path("item");
            if (items.isArray()) {
                for (JsonNode part : items) {
                    JsonNode nested = nestedContent(part);
                    collectMedia(nested, text(part, "type", "text"), refs);
                }
            }
        }
        return refs;
    }

    private void collectMedia(JsonNode payload, String msgType, List<MediaRef> refs) {
        if (payload == null || !payload.isObject()) {
            return;
        }
        String fileId = firstNonBlank(text(payload, "sdkfileid", ""), text(payload, "sdk_file_id", ""));
        if (fileId.isBlank()) {
            return;
        }
        refs.add(new MediaRef(fileId, msgType, text(payload, "filename", "")));
    }

    private String joinMixed(JsonNode mixed) {
        List<String> parts = new ArrayList<>();
        JsonNode items = mixed.path("item");
        if (items.isArray()) {
            for (JsonNode part : items) {
                String pieceType = text(part, "type", "text");
                JsonNode nested = nestedContent(part);
                com.fasterxml.jackson.databind.node.ObjectNode wrapper = mapper.createObjectNode();
                wrapper.put("msgtype", pieceType);
                wrapper.set(pieceType, nested);
                String text = contentFrom(wrapper, pieceType);
                if (!text.isBlank()) {
                    parts.add(text);
                }
            }
        }
        return String.join("\n", parts);
    }

    private JsonNode nestedContent(JsonNode part) {
        JsonNode nested = part.get("content");
        if (nested != null && nested.isTextual()) {
            try {
                return mapper.readTree(nested.asText());
            } catch (Exception ignored) {
                return mapper.createObjectNode().put("content", nested.asText());
            }
        }
        if (nested != null) {
            return nested;
        }
        return mapper.createObjectNode();
    }

    private static String mediaPlaceholder(String kind, JsonNode payload) {
        return ("[" + kind + " " + mediaId(payload) + "]").strip();
    }

    private static String mediaId(JsonNode payload) {
        String path = firstNonBlank(text(payload, "path", ""), text(payload, "local_path", ""));
        if (!path.isBlank()) {
            return "path=" + path;
        }
        String fileId = firstNonBlank(text(payload, "sdkfileid", ""), text(payload, "sdk_file_id", ""));
        if (!fileId.isBlank()) {
            return "sdkfileid=" + fileId;
        }
        return "pending";
    }

    private static long readMsgTime(JsonNode root) {
        JsonNode node = root.get("msgtime");
        if (node == null || node.isNull()) {
            node = root.get("time");
        }
        return node == null || node.isNull() ? 0L : node.asLong();
    }

    private static List<String> stringList(JsonNode node) {
        List<String> values = new ArrayList<>();
        if (node != null && node.isArray()) {
            for (JsonNode item : node) {
                if (item != null && !item.asText("").isBlank()) {
                    values.add(item.asText());
                }
            }
        }
        return values;
    }

    private static String text(JsonNode node, String field, String fallback) {
        if (node == null || node.isMissingNode() || node.isNull()) {
            return fallback;
        }
        JsonNode value = node.get(field);
        if (value == null || value.isNull()) {
            return fallback;
        }
        String text = value.asText("");
        return text == null || text.isBlank() ? fallback : text;
    }

    private static String firstNonBlank(String... values) {
        if (values == null) {
            return "";
        }
        for (String value : values) {
            if (value != null && !value.isBlank()) {
                return value;
            }
        }
        return "";
    }
}
