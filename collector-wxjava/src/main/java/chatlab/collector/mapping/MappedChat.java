package chatlab.collector.mapping;

import java.util.List;

public record MappedChat(UnifiedMessage message, List<MediaRef> media) {
}
