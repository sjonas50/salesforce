@IsTest
private class LeadScoringServiceTest {
    @TestSetup
    static void makeData() {
        Lead l = new Lead(LastName = 'Test', Company = 'Acme', Test_Flag__c = true);
        insert l;
    }

    @IsTest
    static void scoresLead() {
        Lead l = [SELECT Id, Test_Flag__c FROM Lead LIMIT 1];
        Test.startTest();
        Decimal s = LeadScoringService.score(l.Id);
        Test.stopTest();
        System.assertNotEquals(null, s);
    }
}
