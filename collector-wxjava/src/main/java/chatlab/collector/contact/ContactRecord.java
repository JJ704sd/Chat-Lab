package chatlab.collector.contact;

public record ContactRecord(String userId, String name, String corpName, String userType) {

    public static ContactRecord unknown(String userId) {
        String id = userId == null ? "" : userId;
        return new ContactRecord(id, id.isBlank() ? "unknown" : id, "", "unknown");
    }
}
