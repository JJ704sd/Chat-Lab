using System.Text.Json;
using WeComArchive.Collector.Mapping;
using WeComArchive.Collector.Storage;

var fixture = GetOption("--fixture");
var displayMapPath = GetOption("--display-map") ?? "samples/userid_display.json";
var inbox = GetOption("--inbox") ?? "data/wecom/inbox";
var dbPath = GetOption("--db") ?? "data/wecom/collector.sqlite";
var corpId = Environment.GetEnvironmentVariable("WECOM_CORP_ID") ?? "fixture";

if (string.IsNullOrWhiteSpace(fixture))
{
    Console.Error.WriteLine("Live Finance SDK mode needs WECOM_CORP_ID, WECOM_CHAT_SECRET, WECOM_RSA_PRIVATE_KEY_PATH, and WeWorkFinanceSdk.dll.");
    Console.Error.WriteLine("Until those exist, run fixture mode:");
    Console.Error.WriteLine("  dotnet run --project collector/WeComArchive.Collector -- --fixture samples/wecom_archive_decrypted.jsonl");
    return 2;
}

var displayMap = ArchiveMapper.LoadDisplayMap(displayMapPath);
Directory.CreateDirectory(inbox);
var db = new CollectorDb(dbPath);
var written = 0;
var maxSeq = db.GetLastSeq(corpId);
var stamp = DateTime.UtcNow.ToString("yyyyMMddTHHmmss");
var outPath = Path.Combine(inbox, $"archive-{stamp}.jsonl");
using (var output = new StreamWriter(outPath))
{
    foreach (var line in File.ReadLines(fixture))
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            continue;
        }
        using var doc = JsonDocument.Parse(line);
        var mapped = ArchiveMapper.ToJsonl(doc.RootElement, displayMap);
        if (mapped is null)
        {
            continue;
        }
        var seq = ArchiveMapper.ReadSeq(doc.RootElement);
        var msgid = mapped.Value.GetProperty("source_message_id").GetString() ?? "";
        var conversationId = mapped.Value.GetProperty("conversation_id").GetString() ?? "";
        db.UpsertMessage(msgid, seq, conversationId, line, mapped.Value.GetRawText());
        if (seq > maxSeq)
        {
            maxSeq = seq;
        }
        output.WriteLine(mapped.Value.GetRawText());
        written += 1;
    }
}
db.SetLastSeq(corpId, maxSeq);
Console.WriteLine($"wrote\t{written}\t{outPath}\tseq={maxSeq}");
return 0;

static string? GetOption(string name)
{
    var args = Environment.GetCommandLineArgs();
    for (var i = 0; i < args.Length - 1; i += 1)
    {
        if (args[i] == name)
        {
            return args[i + 1];
        }
    }
    return null;
}
