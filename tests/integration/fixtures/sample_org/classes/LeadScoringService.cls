/**
 * Calls the external scoring API through a Named Credential. Exposed to LWC
 * (@AuraEnabled) and to Flow (@InvocableMethod).
 */
public with sharing class LeadScoringService {
    public class ScoreRequest {
        @InvocableVariable(required=true) public Id leadId;
    }

    @AuraEnabled(cacheable=false)
    public static Decimal score(Id leadId) {
        Lead l = [SELECT Id, Email, Industry, AnnualRevenue, Country__c FROM Lead WHERE Id = :leadId];
        HttpRequest req = new HttpRequest();
        req.setEndpoint('callout:ScoringAPI/v2/score');
        req.setMethod('POST');
        req.setBody(JSON.serialize(l));
        Http http = new Http();
        HttpResponse res = http.send(req);
        if (res.getStatusCode() != 200) {
            throw new CalloutException('Scoring failed: ' + res.getStatus());
        }
        Map<String, Object> body = (Map<String, Object>) JSON.deserializeUntyped(res.getBody());
        return (Decimal) body.get('score');
    }

    /**
     * Flows call this after the record is saved, so the callout cannot run in the
     * same transaction ("uncommitted work pending"): queue it and let the job write
     * Score__c back.
     */
    @InvocableMethod(label='Score Lead' category='Lead')
    public static List<Decimal> scoreForFlow(List<ScoreRequest> requests) {
        List<Id> ids = new List<Id>();
        for (ScoreRequest r : requests) {
            ids.add(r.leadId);
        }
        System.enqueueJob(new ScoreJob(ids));
        List<Decimal> out = new List<Decimal>();
        for (Id i : ids) {
            out.add(null);
        }
        return out;
    }

    public class ScoreJob implements Queueable, Database.AllowsCallouts {
        private final List<Id> leadIds;
        public ScoreJob(List<Id> leadIds) {
            this.leadIds = leadIds;
        }
        public void execute(QueueableContext ctx) {
            List<Lead> updates = new List<Lead>();
            for (Id leadId : leadIds) {
                updates.add(new Lead(Id = leadId, Score__c = score(leadId)));
            }
            update updates;
        }
    }
}
