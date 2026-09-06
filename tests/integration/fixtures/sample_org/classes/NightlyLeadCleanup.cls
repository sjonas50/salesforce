/**
 * Deletes stale unconverted leads. Scheduled nightly (see CronTrigger dump).
 */
global class NightlyLeadCleanup implements Database.Batchable<SObject>, Schedulable {
    global Database.QueryLocator start(Database.BatchableContext bc) {
        return Database.getQueryLocator('SELECT Id FROM Lead WHERE IsConverted = false AND Status = \'Dead\' AND LastModifiedDate < LAST_N_DAYS:365');
    }

    global void execute(Database.BatchableContext bc, List<Lead> scope) {
        delete scope;
    }

    global void finish(Database.BatchableContext bc) {
        Cleanup_Settings__c settings = Cleanup_Settings__c.getOrgDefaults();
        if (settings != null && settings.Notify__c) {
            System.enqueueJob(new CleanupNotifier());
        }
    }

    global void execute(SchedulableContext sc) {
        Database.executeBatch(new NightlyLeadCleanup(), 200);
    }
}
