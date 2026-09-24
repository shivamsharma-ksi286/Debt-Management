// Copyright (c) 2026, Ksolves India Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Loan Provider", {
	setup(frm) {
		// Each grid row names its own company, so the account pickers are
		// filtered per row rather than per form.
		const row_account = (root_type) => (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { company: row.company, root_type, is_group: 0 } };
		};

		frm.set_query("loan_account", "company_accounts", row_account("Liability"));
		frm.set_query("interest_expense_account", "company_accounts", row_account("Expense"));
	},
});

frappe.ui.form.on("Loan Provider Account", {
	company(frm, cdt, cdn) {
		// The accounts that were valid for the previous company almost
		// certainly are not valid for the new one.
		frappe.model.set_value(cdt, cdn, { loan_account: null, interest_expense_account: null });
		frm.refresh_field("company_accounts");
	},
});
