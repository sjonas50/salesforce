trigger OpportunityDiscount on Opportunity (after update) {
    for (Opportunity opp : Trigger.new) {
        Opportunity old = Trigger.oldMap.get(opp.Id);
        if (opp.Discount__c != old.Discount__c) {
            OpportunityDiscountService.submitForApproval(opp);
        }
    }
}
