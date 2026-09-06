"""C20 Apex analyzer: references, SOQL, DML, callouts, entry points."""

from __future__ import annotations

from offramp.extract.apex import analyze

HANDLER = """
public with sharing class LeadRoutingHandler implements TriggerAction {
    // comment with Fake.Class() reference that must be ignored
    public void execute(List<SObject> newRecords, Map<Id, SObject> oldMap) {
        String s = 'Ignored.InString()';
        Map<String, Territory__c> byCountry = new Map<String, Territory__c>();
        for (Territory__c t : [SELECT Id, Country_Code__c, Owner__c FROM Territory__c WHERE Country_Code__c IN :countries]) {
            byCountry.put(t.Country_Code__c, t);
        }
        List<Lead> toUpdate = new List<Lead>();
        Lead l = (Lead) newRecords[0];
        l.Score__c = LeadScoringService.score(l.Id);
        toUpdate.add(l);
        Database.update(toUpdate, false);
        System.enqueueJob(new CleanupNotifier());
        HttpRequest req = new HttpRequest();
        req.setEndpoint('callout:ScoringAPI/v2/score');
        Type t2 = Type.forName('DynamicHandler');
        String lbl = Label.Lead_Email_Required;
        Cleanup_Settings__c cs = Cleanup_Settings__c.getOrgDefaults();
        if (l.Email == null) { return; }
    }
}
"""


def test_header_and_entry_points() -> None:
    a = analyze(HANDLER)
    assert a.name == "LeadRoutingHandler"
    assert a.kind == "class"
    assert a.sharing == "with"
    assert a.implements == ["TriggerAction"]
    assert "trigger_handler" in a.entry_points


def test_class_references_exclude_comments_strings_and_platform_types() -> None:
    a = analyze(HANDLER)
    assert "LeadScoringService" in a.class_references
    assert "CleanupNotifier" in a.class_references
    assert "TriggerAction" in a.class_references
    assert "Fake" not in a.class_references
    assert "Ignored" not in a.class_references
    assert "HttpRequest" not in a.class_references
    assert "Database" not in a.class_references
    assert "LeadRoutingHandler" not in a.class_references
    assert "LeadScoringService.score" in a.method_calls


def test_soql_and_fields() -> None:
    a = analyze(HANDLER)
    assert len(a.soql) == 1
    q = a.soql[0]
    assert q.sobject == "Territory__c"
    assert set(q.fields) == {"Id", "Country_Code__c", "Owner__c"}
    assert q.where_fields == ["Country_Code__c"]
    assert "Territory__c.Country_Code__c" in a.field_references
    assert "Lead.Score__c" in a.field_references
    assert "Lead.Email" in a.field_references
    # Member names must not leak into the sObject set.
    assert "Score__c" not in a.sobject_references
    assert "Country_Code__c" not in a.sobject_references
    assert {"Lead", "Territory__c", "Cleanup_Settings__c"} <= set(a.sobject_references)


def test_dml_callouts_async_dynamic() -> None:
    a = analyze(HANDLER)
    assert [(d.op, d.sobject, d.via_database_class) for d in a.dml] == [("update", "Lead", True)]
    assert a.callouts == ["HttpRequest"]
    assert a.named_credentials == ["ScoringAPI"]
    assert [(x.mechanism, x.target_class) for x in a.async_calls] == [
        ("enqueue", "CleanupNotifier")
    ]
    assert a.type_forname_literals == ["DynamicHandler"]
    assert a.custom_labels == ["Lead_Email_Required"]
    assert a.custom_settings == ["Cleanup_Settings__c"]


def test_trigger_header() -> None:
    a = analyze(
        "trigger LeadDispatcher on Lead (before insert, after update) { MetadataTriggerHandler.run(); }"
    )
    assert a.kind == "trigger"
    assert a.trigger_object == "Lead"
    assert a.trigger_events == ["before insert", "after update"]
    assert a.class_references == ["MetadataTriggerHandler"]
    assert "trigger" in a.entry_points


def test_batchable_schedulable_and_dynamic_soql() -> None:
    src = """
    global class Nightly implements Database.Batchable<SObject>, Schedulable {
        global Database.QueryLocator start(Database.BatchableContext bc) {
            return Database.getQueryLocator('SELECT Id FROM Lead WHERE Status = \\'Dead\\' AND IsConverted = false');
        }
        global void execute(Database.BatchableContext bc, List<Lead> scope) { delete scope; }
        global void finish(Database.BatchableContext bc) { Database.executeBatch(new Nightly(), 200); }
        global void execute(SchedulableContext sc) {}
    }
    """
    a = analyze(src)
    assert {"batchable", "schedulable"} <= set(a.entry_points)
    q = next(x for x in a.soql if x.dynamic)
    assert q.sobject == "Lead"
    assert set(q.where_fields) == {"Status", "IsConverted"}
    assert [(d.op, d.sobject) for d in a.dml] == [("delete", "Lead")]
    assert "Database.Batchable" not in a.class_references
    assert "Schedulable" not in a.class_references


def test_annotations_mark_entry_points() -> None:
    src = """
    public with sharing class Svc {
        @AuraEnabled(cacheable=true) public static Lead get(Id i) { return [SELECT Id FROM Lead WHERE Id = :i]; }
        @InvocableMethod(label='x') public static void inv(List<Id> ids) {}
        @future(callout=true) public static void later() {}
    }
    """
    a = analyze(src)
    assert {"aura_enabled", "invocable", "future"} <= set(a.entry_points)


def test_lowercase_qualifiers_are_candidate_class_references() -> None:
    """Apex is case-insensitive: ``customerServices.get()`` may be a static call on CustomerServices."""
    from offramp.extract.apex import analyze

    src = """@isTest
    public class CustomerServicesTest {
        @isTest static void t() {
            customerServices.Customer c = customerServices.getCustomerFields('Lead');
            List<Market__c> ms = testDataFactory.makeMarkets(3);
            String s = name.toLowerCase();
        }
    }"""
    a = analyze(src, name_hint="CustomerServicesTest")
    assert "customerServices" in a.candidate_class_references
    assert "testDataFactory" in a.candidate_class_references
    assert "name" in a.candidate_class_references  # a variable; the builder drops it
    assert "CustomerServices" not in a.class_references  # never claimed outright
