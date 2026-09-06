/**
 * Reads whatever field an admin configures; static analysis cannot see the targets.
 */
public with sharing class DynamicFieldReader {
    public static Object read(Id recordId, String fieldName) {
        Schema.SObjectType t = recordId.getSObjectType();
        String soql = 'SELECT ' + fieldName + ' FROM ' + t.getDescribe().getName() + ' WHERE Id = :recordId';
        SObject so = Database.query(soql);
        Map<String, Schema.SObjectType> all = Schema.getGlobalDescribe();
        Lead l = (Lead) so;
        Object rating = l.get('Rating');
        return so.get(fieldName);
    }
}
