import { LightningElement, api, wire } from 'lwc';
import getLead from '@salesforce/apex/LeadController.getLead';
import scoreLead from '@salesforce/apex/LeadScoringService.score';

export default class LeadCard extends LightningElement {
    @api recordId;
    lead;
    score;

    @wire(getLead, { leadId: '$recordId' })
    wiredLead({ data, error }) {
        if (data) {
            this.lead = data;
            // Imperative call computes a derived value from the wired payload.
            scoreLead({ leadId: this.recordId }).then((s) => {
                this.score = s;
            });
        } else if (error) {
            console.error(error);
        }
    }

    get isHighValue() {
        if (this.score && this.score > 80) {
            return true;
        }
        return false;
    }
}
