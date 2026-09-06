trigger LeadDispatcher on Lead (before insert, before update, after insert, after update) {
    MetadataTriggerHandler.run();
}
