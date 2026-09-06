public with sharing class OpportunityDiscountService {
    public static void submitForApproval(Opportunity opp) {
        if (opp.Discount__c > 20 && opp.Amount > 100000) {
            Approval.ProcessSubmitRequest req = new Approval.ProcessSubmitRequest();
            req.setObjectId(opp.Id);
            req.setProcessDefinitionNameOrId('HighValueDiscount');
            Approval.process(req);
        }
    }

    public static Decimal discountedAmount(Opportunity opp) {
        return opp.Amount * (1 - opp.Discount__c / 100);
    }
}
