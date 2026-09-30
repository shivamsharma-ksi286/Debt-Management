// Copyright (c) 2026, Ksolves India Limited and contributors
// For license information, please see license.txt

frappe.query_reports["Debt Facility Summary"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "loan_provider",
			label: __("Loan Provider"),
			fieldtype: "Link",
			options: "Loan Provider",
		},
		{
			fieldname: "provider_type",
			label: __("Provider Type"),
			fieldtype: "Select",
			options: ["", "Bank", "NBFC", "Inter-Corporate", "Related Party", "Other"],
		},
		{
			fieldname: "loan_type",
			label: __("Loan Type"),
			fieldtype: "Select",
			options: [
				"",
				"Term Loan",
				"Working Capital Term Loan",
				"COVID Loan",
				"Channel Finance",
				"Peak Season",
				"Inter-Corporate Deposit",
				"Other",
			],
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: ["", "Draft", "Active", "Closed", "Foreclosed", "Cancelled"],
			default: "Active",
		},
		{
			fieldname: "in_scope",
			label: __("In Scope"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "next_due_up_to",
			label: __("Next Due Up To"),
			fieldtype: "Date",
		},
	],

	formatter(value, row, column, data, default_formatter) {
		const formatted = default_formatter(value, row, column, data);

		// A due date already behind us is money the bank is waiting for, so it
		// should be the first thing the eye lands on.
		if (column.fieldname === "next_due_date" && value && value < frappe.datetime.get_today()) {
			return `<span style="color: var(--red-500); font-weight: 600;">${formatted}</span>`;
		}

		return formatted;
	},
};
