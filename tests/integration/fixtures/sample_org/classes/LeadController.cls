public with sharing class LeadController {
    @AuraEnabled(cacheable=true)
    public static Lead getLead(Id leadId) {
        return [SELECT Id, Name, Email, Company, Status, Score__c, Owner.Name FROM Lead WHERE Id = :leadId WITH SECURITY_ENFORCED];
    }
}
