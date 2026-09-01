package chatlab.collector.config;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertTrue;

class SdkLibPathTest {

    @TempDir
    Path tempDir;

    @Test
    void scansDirectoryAndPutsLibFirst() throws Exception {
        Files.writeString(tempDir.resolve("WeWorkFinanceSdk.dll"), "x");
        Files.writeString(tempDir.resolve("libcurl-x64.dll"), "x");
        Files.writeString(tempDir.resolve("libssl-1_1-x64.dll"), "x");
        String path = SdkLibPath.resolve("", tempDir);
        assertTrue(path.contains("/libcurl-x64.dll,") || path.contains("/libssl-1_1-x64.dll,"));
        int lib = Math.min(path.indexOf("libcurl-x64.dll"), path.indexOf("libssl-1_1-x64.dll"));
        int sdk = path.indexOf("WeWorkFinanceSdk.dll");
        assertTrue(lib >= 0 && sdk > lib);
    }
}
