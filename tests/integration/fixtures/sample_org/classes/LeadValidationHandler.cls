public with sharing class LeadValidationHandler implements TriggerAction {
    public void execute(List<SObject> newRecords, Map<Id, SObject> oldMap) {
        for (SObject so : newRecords) {
            Lead l = (Lead) so;
            if (String.isBlank(l.Email) && l.LeadSource == 'Web') {
                l.addError(Label.Lead_Email_Required);
            }
            if (l.Country__c == null && l.Territory__c != null) {
                Territory__c terr = [SELECT Country_Code__c FROM Territory__c WHERE Id = :l.Territory__c LIMIT 1];
                l.Country__c = terr.Country_Code__c;
            }
        }
    }
}
