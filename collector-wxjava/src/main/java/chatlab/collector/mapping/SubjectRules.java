package chatlab.collector.mapping;

import java.text.Normalizer;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Q3 subject identity. Body {@code @mentions} are never consulted.
 */
public final class SubjectRules {

    private static final Pattern SUFFIX = Pattern.compile("(?:^|\\s)@(?<subject>[^@\\s]+)\\s*$");
    private static final Set<String> ZHONGJI = Set.of(normalize("中技"), normalize("中技物流"));

    private SubjectRules() {
    }

    public static String resolve(String senderName, String senderCorpName) {
        if (isZhongjiCorp(senderCorpName)) {
            return "zhongji";
        }
        String display = displayName(senderName, senderCorpName);
        Matcher matcher = SUFFIX.matcher(display == null ? "" : display);
        if (matcher.find() && isZhongjiCorp(stripPunct(matcher.group("subject")))) {
            return "zhongji";
        }
        return "other";
    }

    public static String displayName(String senderName, String senderCorpName) {
        String name = senderName == null || senderName.isBlank() ? "unknown" : senderName.strip();
        if (senderCorpName != null && !senderCorpName.isBlank()) {
            return name + " @" + senderCorpName.strip();
        }
        return name;
    }

    public static boolean isZhongjiCorp(String corpOrSuffix) {
        if (corpOrSuffix == null || corpOrSuffix.isBlank()) {
            return false;
        }
        String normalized = normalize(corpOrSuffix);
        return ZHONGJI.contains(normalized) || normalized.startsWith(normalize("中技"));
    }

    static String normalize(String value) {
        String folded = Normalizer.normalize(value, Normalizer.Form.NFKC).strip().toLowerCase(Locale.ROOT);
        return folded.replaceAll("[\\s_-]+", "");
    }

    private static String stripPunct(String value) {
        return value.replaceAll("[，,。.;；:：()（）\\[\\]【】]", "");
    }
}
