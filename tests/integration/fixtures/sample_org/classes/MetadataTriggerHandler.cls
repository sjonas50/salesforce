/**
 * Generic dispatcher: reads Trigger_Action__mdt rows for the current object +
 * event and invokes each handler class by name. Static analysis alone sees no
 * handler classes here; the CMT reader (C2) recovers the real graph.
 */
public with sharing class MetadataTriggerHandler {
    private static Boolean bypass = false;

    public static void run() {
        if (bypass) {
            return;
        }
        String objectName = Trigger.new != null
            ? Trigger.new[0].getSObjectType().getDescribe().getName()
            : Trigger.old[0].getSObjectType().getDescribe().getName();
        String eventName = String.valueOf(Trigger.operationType);
        List<Trigger_Action__mdt> actions = [
            SELECT Apex_Class__c, Order__c
            FROM Trigger_Action__mdt
            WHERE Object__c = :objectName AND Trigger_Event__c = :eventName
            ORDER BY Order__c ASC
        ];
        for (Trigger_Action__mdt action : actions) {
            Type t = Type.forName(action.Apex_Class__c);
            if (t == null) {
                throw new HandlerException('Unknown handler ' + action.Apex_Class__c);
            }
            TriggerAction handler = (TriggerAction) t.newInstance();
            handler.execute(Trigger.new, Trigger.oldMap);
        }
    }

    public static void setBypass(Boolean value) {
        bypass = value;
    }

    public class HandlerException extends Exception {}
}
