package chatlab.collector.mapping;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class SubjectRulesTest {

    @Test
    void corpNameZhongjiIsZhongji() {
        assertEquals("zhongji", SubjectRules.resolve("张浩", "中技物流"));
        assertEquals("zhongji", SubjectRules.resolve("张浩", "中技"));
    }

    @Test
    void displaySuffixZhongjiIsZhongji() {
        assertEquals("zhongji", SubjectRules.resolve("张浩 @中技物流", ""));
        assertEquals("zhongji", SubjectRules.resolve("张某 @中技", null));
    }

    @Test
    void otherCorpIsOther() {
        assertEquals("other", SubjectRules.resolve("客服", "嘉航盛物流"));
        assertEquals("other", SubjectRules.resolve("客服 @嘉航盛物流", ""));
    }

    @Test
    void missingCorpFallsBackToOther() {
        assertEquals("other", SubjectRules.resolve("张浩", ""));
    }

    @Test
    void displayNameJoinsCorpSuffix() {
        assertEquals("张浩 @中技物流", SubjectRules.displayName("张浩", "中技物流"));
    }
}
