({
    doInit: function (component, event, helper) {
        var action = component.get("c.getLeadScore");
        action.setParams({ leadId: component.get("v.recordId") });
        action.setCallback(this, function (response) {
            if (response.getState() === "SUCCESS") {
                component.set("v.score", response.getReturnValue());
            }
        });
        $A.enqueueAction(action);
    },
    startCapture: function (component) {
        var flow = component.find("captureFlow");
        flow.startFlow("CaptureLeadDetails", [{ name: "lead", type: "SObject", value: null }]);
    }
})
