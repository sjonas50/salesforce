public class CleanupNotifier implements Queueable {
    public void execute(QueueableContext ctx) {
        EventBus.publish(new CaseEscalation__e(Reason__c = 'cleanup-complete'));
    }
}
