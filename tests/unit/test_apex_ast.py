"""Grammar-backed Apex analysis (AD-31): what the tree gives that tokens cannot.

Skipped when Node or ``tools/apex-parser`` is not installed (``make apex-parser``).
"""

from __future__ import annotations

import pytest

from offramp.extract.apex import ApexAnalysis, analyze, ast_bridge, selected_engine

pytestmark = pytest.mark.skipif(
    not ast_bridge.is_available(), reason="apex parser not installed (make apex-parser)"
)


def _ast(src: str, name: str | None = None) -> ApexAnalysis:
    a = analyze(src, name_hint=name, engine="ast")
    assert a.engine == "ast" and a.parse_errors == 0
    return a


def test_engine_selection_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OFFRAMP_APEX_ENGINE", raising=False)
    assert selected_engine("ast") == "ast"
    assert selected_engine("tokenizer") == "tokenizer"
    assert selected_engine(None) == "ast"
    assert ast_bridge.get_server().version()["parser"].startswith("5.")


def test_enum_constants_and_inner_types_are_not_class_references() -> None:
    src = """
    public class CRLP_Rollup_TEST {
        public enum TestType { TestTrigger, TestQueueuable, TestBatch }
        public class Wrapper { public String label; }
        private static final String CONST = 'x';
        static void run(TestType tt) {
            if (tt == TestType.TestBatch) { System.debug(CONST.toLowerCase()); }
            Wrapper w = new Wrapper();
            switch on tt { when TestTrigger { } when else { } }
            String s = Label.Rollup_Error;   // not shadowed by Wrapper.label
        }
    }
    """
    a = _ast(src)
    assert a.class_references == []
    assert a.inner_types == ["TestType", "Wrapper"]
    assert a.custom_labels == ["Rollup_Error"]
    assert a.candidate_class_references == []


def test_variables_are_typed_by_declaration_not_by_name() -> None:
    """``address.MailingCity__c`` is a field of the variable's type, not of the Address object."""
    src = """
    public class ADDR_Addresses_TDTM {
        private Address__c address;
        public void run(List<Contact> contacts, Map<Id, Account> accounts) {
            for (Address__c address : [SELECT Id FROM Address__c]) {
                address.MailingCity__c = 'x';
            }
            String city = this.address.MailingCity__c;
            Account acc = accounts.get(contacts[0].AccountId);
            acc.Name = 'y';
            Contact c = (Contact) contacts.get(0);
            c.put('Email', 'e@x');
        }
    }
    """
    a = _ast(src)
    assert "Address__c.MailingCity__c" in a.field_references
    assert "Address__c.MailingCity__c" in a.field_writes
    assert "address" not in a.sobject_references
    assert {"Account.Name", "Contact.AccountId", "Contact.Email"} <= set(a.field_references)
    assert {"Account.Name", "Contact.Email"} <= set(a.field_writes)


def test_member_named_like_another_class_does_not_hide_that_class() -> None:
    src = """
    public class ContactAdapter {
        private ContactSelector contactSelector {
            get { if (contactSelector == null) { contactSelector = new ContactSelector(); } return contactSelector; }
            set;
        }
        private static OrgConfig orgConfig = new OrgConfig();
        public List<Contact> load() { orgConfig.refresh(); return contactSelector.selectAll(); }
    }
    """
    a = _ast(src)
    assert {"ContactSelector", "OrgConfig"} <= set(a.class_references)
    assert {"ContactSelector.selectAll", "OrgConfig.refresh"} <= set(a.method_calls)


def test_variable_type_beats_case_insensitive_class_candidate() -> None:
    """The tokenizer guessed ``contactService.x()`` → class ContactService; the tree knows better."""
    src = """
    public class BDI_DataImportService {
        @TestVisible private BDI_ContactService contactService { get; private set; }
        public void run() { contactService.importContacts(); }
    }
    """
    a = _ast(src)
    assert a.class_references == ["BDI_ContactService"]
    assert a.candidate_class_references == []


def test_trigger_context_variables_are_typed() -> None:
    src = """
    trigger LeadTrigger on Lead (before insert, after update) {
        for (Lead l : Trigger.new) { l.Status = 'Working'; }
        Trigger.newMap.get(Trigger.new[0].Id).Company = 'Acme';
        Handler.run(Trigger.new);
    }
    """
    a = _ast(src)
    assert a.kind == "trigger" and a.trigger_object == "Lead"
    assert a.trigger_events == ["before insert", "after update"]
    assert {"Lead.Status", "Lead.Company", "Lead.Id"} <= set(a.field_references)
    assert {"Lead.Status", "Lead.Company"} <= set(a.field_writes)
    assert a.class_references == ["Handler"]


def test_dml_on_new_expressions_and_database_calls() -> None:
    src = """
    public class Svc {
        public void run(List<Opportunity> opps) {
            insert new Task(Subject = 's', WhatId = opps[0].Id);
            upsert opps Opportunity.External_Id__c;
            Database.SaveResult[] r = Database.insert(opps, false);
            delete [SELECT Id FROM Lead WHERE IsConverted = true];
            List<SObject> rows = Database.query(buildQuery());
            System.enqueueJob(new ScoreJob(opps));
            ScoreJob job = new ScoreJob(opps);
            System.enqueueJob(job);
        }
    }
    """
    a = _ast(src)
    assert [(d.op, d.sobject, d.via_database_class) for d in a.dml] == [
        ("insert", "Task", False),
        ("upsert", "Opportunity", False),
        ("insert", "Opportunity", True),
        ("delete", "Lead", False),
    ]
    assert "Opportunity.External_Id__c" in a.field_references
    assert {"Task.Subject", "Task.WhatId"} <= set(a.field_writes)
    assert "dynamic_soql" in a.dynamic_access
    assert [x.target_class for x in a.async_calls] == ["ScoreJob", "ScoreJob"]


def test_schema_describe_and_sobjecttype_forms() -> None:
    src = """
    public class Describe {
        Schema.DescribeFieldResult f = Schema.SObjectType.Opportunity.fields.Amount;
        Map<String, Schema.SObjectField> m = Schema.SObjectType.Contact.fields.getMap();
        String n = DataImportBatch__c.SObjectType.class.getName();
        Schema.SObjectType t = Data_Import_Settings__c.SobjectType;
        String lbl = System.Label.Other_Label;
        Cleanup_Settings__c cs = Cleanup_Settings__c.getOrgDefaults();
    }
    """
    a = _ast(src)
    assert "Opportunity.Amount" in a.field_references
    assert "Contact.getMap" not in a.field_references
    assert {"DataImportBatch__c", "Data_Import_Settings__c", "Contact", "Opportunity"} <= set(
        a.sobject_references
    )
    assert not any("SObjectType" in c or "SobjectType" in c for c in a.class_references)
    assert a.custom_labels == ["Other_Label"]
    assert a.custom_settings == ["Cleanup_Settings__c"]


def test_syntax_errors_fall_back_to_the_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OFFRAMP_APEX_ENGINE", raising=False)
    a = analyze("public class Broken { void m( { Lead l; insert l; }", name_hint="Broken")
    assert a.engine == "tokenizer"
    assert a.parse_errors > 0
    assert a.name == "Broken"
