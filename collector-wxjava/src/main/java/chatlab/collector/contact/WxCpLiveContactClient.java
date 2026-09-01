package chatlab.collector.contact;

import chatlab.collector.config.CollectorProperties;
import me.chanjar.weixin.cp.api.WxCpService;
import me.chanjar.weixin.cp.bean.WxCpUser;
import me.chanjar.weixin.cp.bean.external.contact.WxCpExternalContactInfo;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Optional;

public class WxCpLiveContactClient implements ContactCacheService.LiveContactClient {

    private static final Logger log = LoggerFactory.getLogger(WxCpLiveContactClient.class);

    private final WxCpService wxCpService;
    private final CollectorProperties properties;

    public WxCpLiveContactClient(WxCpService wxCpService, CollectorProperties properties) {
        this.wxCpService = wxCpService;
        this.properties = properties;
    }

    @Override
    public Optional<ContactRecord> lookup(String userId, boolean external) throws Exception {
        if (external) {
            WxCpExternalContactInfo info = wxCpService.getExternalContactService().getContactDetail(userId, null);
            if (info == null || info.getExternalContact() == null) {
                return Optional.empty();
            }
            var contact = info.getExternalContact();
            String name = firstNonBlank(contact.getName(), userId);
            String corp = firstNonBlank(contact.getCorpName(), "");
            return Optional.of(new ContactRecord(userId, name, corp, "external"));
        }
        WxCpUser user = wxCpService.getUserService().getById(userId);
        if (user == null) {
            return Optional.empty();
        }
        String name = firstNonBlank(user.getName(), userId);
        String corp = firstNonBlank(properties.getOwnCorpName(), "");
        return Optional.of(new ContactRecord(userId, name, corp, "internal"));
    }

    private static String firstNonBlank(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }
}
