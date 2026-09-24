// Copyright (c) 2026, Ksolves India Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Debt Facility", {
	setup(frm) {
		// Accounts are company-scoped in ERPNext, so the picker has to be too.
		// The server re-checks this; the filter just stops the wrong account
		// being offered in the first place.
		const company_account = (extra = {}) => () => ({
			filters: { company: frm.doc.company, is_group: 0, ...extra },
		});

		frm.set_query("loan_liability_account", company_account({ root_type: "Liability" }));
		frm.set_query("interest_expense_account", company_account({ root_type: "Expense" }));
		frm.set_query("bank_gl_account", company_account());

		frm.set_query("loan_provider", () => ({ filters: { is_active: 1 } }));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 1 && !frm.doc.is_revolving) {
			frm.add_custom_button(__("Create Repayment Schedule"), () =>
				frm.trigger("create_repayment_schedule")
			).addClass("btn-primary");
		}
	},

	create_repayment_schedule(frm) {
		frappe.new_doc("Debt Repayment Schedule", {
			company: frm.doc.company,
			loan_provider: frm.doc.loan_provider,
			loan: frm.doc.name,
		});
	},

	company(frm) {
		// Carrying accounts across a company change would post into the
		// previous company's ledger; the server rejects that, so clear them
		// and let the provider defaults refill for the new company.
		["loan_liability_account", "interest_expense_account", "bank_gl_account"].forEach((field) =>
			frm.set_value(field, null)
		);
	},

	benchmark_rate(frm) {
		frm.trigger("recalculate_floating_rate");
	},

	spread(frm) {
		frm.trigger("recalculate_floating_rate");
	},

	recalculate_floating_rate(frm) {
		// Only for floating facilities, and only when the user is actively
		// editing an input -- the server never overwrites a negotiated rate on
		// its own, so this is the one place the arithmetic is reapplied.
		if (frm.doc.interest_rate_type !== "Floating") return;

		const rate = flt(frm.doc.benchmark_rate) + flt(frm.doc.spread);
		if (rate !== flt(frm.doc.rate_of_interest)) {
			frm.set_value("rate_of_interest", rate);
		}
	},

	interest_rate_type(frm) {
		if (frm.doc.interest_rate_type === "Fixed") {
			frm.set_value({ benchmark: null, benchmark_rate: 0, spread: 0 });
		}
	},
});
