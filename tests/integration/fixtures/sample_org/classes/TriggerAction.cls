/**
 * Contract for CMT-dispatched trigger handlers (Trigger Actions Framework style).
 */
public interface TriggerAction {
    void execute(List<SObject> newRecords, Map<Id, SObject> oldMap);
}
