public with sharing class LeadRoutingHandler implements TriggerAction {
    public void execute(List<SObject> newRecords, Map<Id, SObject> oldMap) {
        Set<String> countries = new Set<String>();
        for (SObject so : newRecords) {
            countries.add(((Lead) so).Country__c);
        }
        Map<String, Territory__c> byCountry = new Map<String, Territory__c>();
        for (Territory__c t : [SELECT Id, Country_Code__c, Owner__c, Queue_Id__c FROM Territory__c WHERE Country_Code__c IN :countries]) {
            byCountry.put(t.Country_Code__c, t);
        }
        List<Lead> toUpdate = new List<Lead>();
        for (SObject so : newRecords) {
            Lead l = (Lead) so;
            Territory__c t = byCountry.get(l.Country__c);
            if (t != null) {
                Lead copy = new Lead(Id = l.Id, OwnerId = t.Owner__c != null ? t.Owner__c : t.Queue_Id__c);
                copy.Score__c = LeadScoringService.score(l.Id);
                toUpdate.add(copy);
            }
        }
        if (!toUpdate.isEmpty()) {
            MetadataTriggerHandler.setBypass(true);
            Database.update(toUpdate, false);
            MetadataTriggerHandler.setBypass(false);
        }
    }
}
