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

    @InvocableMethod(label='Score Lead' category='Lead')
    public static List<Decimal> scoreForFlow(List<ScoreRequest> requests) {
        List<Decimal> out = new List<Decimal>();
        for (ScoreRequest r : requests) {
            out.add(score(r.leadId));
        }
        return out;
    }
}
