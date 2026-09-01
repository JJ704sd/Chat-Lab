package chatlab.collector.config;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.stream.Stream;

/**
 * Builds {@code msgAuditLibPath} in the form WxJava expects:
 * {@code <dir>/<lib*.dll>,<other.dll>} so dependency libraries load first.
 */
public final class SdkLibPath {

    private SdkLibPath() {
    }

    public static String resolve(String configured, Path sdkDir) {
        if (configured != null && !configured.isBlank()) {
            return configured.replace('\\', '/');
        }
        if (sdkDir == null || !Files.isDirectory(sdkDir)) {
            return "";
        }
        List<String> libs = new ArrayList<>();
        List<String> others = new ArrayList<>();
        try (Stream<Path> stream = Files.list(sdkDir)) {
            stream.filter(Files::isRegularFile).forEach(path -> {
                String name = path.getFileName().toString();
                String lower = name.toLowerCase(Locale.ROOT);
                if (!(lower.endsWith(".dll") || lower.endsWith(".so"))) {
                    return;
                }
                if (lower.contains("lib")) {
                    libs.add(name);
                } else {
                    others.add(name);
                }
            });
        } catch (IOException ex) {
            throw new IllegalStateException("failed to scan SDK directory " + sdkDir, ex);
        }
        if (libs.isEmpty() && others.isEmpty()) {
            return "";
        }
        List<String> ordered = new ArrayList<>(libs);
        ordered.addAll(others);
        Path joined = sdkDir.toAbsolutePath().normalize().resolve(String.join(",", ordered));
        return joined.toString().replace('\\', '/');
    }
}
