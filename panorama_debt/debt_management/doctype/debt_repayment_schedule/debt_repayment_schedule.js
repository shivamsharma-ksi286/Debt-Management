// Copyright (c) 2026, Ksolves India Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Debt Repayment Schedule", {
	setup(frm) {
		// Only facilities that can actually carry a schedule: submitted, not
		// revolving, and belonging to the company and provider on this form.
		frm.set_query("loan", () => ({
			filters: {
				company: frm.doc.company,
				loan_provider: frm.doc.loan_provider,
				docstatus: 1,
				is_revolving: 0,
			},
		}));
	},

	refresh(frm) {
		frm.trigger("add_draft_buttons");
		frm.trigger("add_submitted_buttons");
	},

	company(frm) {
		frm.trigger("clear_facility");
	},

	loan_provider(frm) {
		frm.trigger("clear_facility");
	},

	clear_facility(frm) {
		// The facility filter just changed underneath the chosen value, so a
		// stale selection would no longer satisfy it.
		if (frm.doc.loan) {
			frm.set_value("loan", null);
		}
	},

	add_draft_buttons(frm) {
		if (frm.doc.docstatus !== 0 || !frm.doc.loan) return;

		frm.add_custom_button(__("Generate Schedule"), () => {
			if (!(frm.doc.repayment_schedule || []).length) {
				frm.trigger("generate_schedule");
				return;
			}
			frappe.confirm(
				__("This replaces all {0} rows in the schedule. Continue?", [
					frm.doc.repayment_schedule.length,
				]),
				() => frm.trigger("generate_schedule")
			);
		}).addClass("btn-primary");

		if ((frm.doc.repayment_schedule || []).length) {
			frm.add_custom_button(__("Clear Schedule"), () => {
				frappe.confirm(__("Remove all rows from the schedule?"), () =>
					frm.call({ method: "clear_schedule", doc: frm.doc, freeze: true }).then(() => {
						frm.refresh_field("repayment_schedule");
						frm.dirty();
					})
				);
			});
		}
	},

	generate_schedule(frm) {
		frm.call({
			method: "generate_schedule",
			doc: frm.doc,
			freeze: true,
			freeze_message: __("Building the amortisation schedule..."),
		}).then((r) => {
			frm.refresh_field("repayment_schedule");
			frm.refresh_field("total_payable");
			frm.dirty();
			if (r.message) {
				frappe.show_alert({
					message: __("Generated {0} instalments.", [r.message]),
					indicator: "green",
				});
			}
		});
	},

	add_submitted_buttons(frm) {
		if (frm.doc.docstatus !== 1) return;

		frm.add_custom_button(__("Mark Instalment Paid"), () =>
			frm.trigger("mark_instalment_paid")
		).addClass("btn-primary");

		frm.add_custom_button(__("Recalculate from Instalment"), () =>
			frm.trigger("recalculate_from")
		);
	},

	mark_instalment_paid(frm) {
		const unpaid = (frm.doc.repayment_schedule || []).filter(
			(row) => !["Paid", "Waived"].includes(row.payment_status)
		);

		if (!unpaid.length) {
			frappe.msgprint({
				title: __("Nothing Outstanding"),
				message: __("Every instalment on this schedule is already settled."),
				indicator: "green",
			});
			return;
		}

		const next = unpaid[0];
		const dialog = new frappe.ui.Dialog({
			title: __("Mark Instalment Paid"),
			fields: [
				{
					fieldname: "instalment_number",
					fieldtype: "Select",
					label: __("Instalment"),
					reqd: 1,
					default: String(next.instalment_number),
					options: unpaid.map((row) => ({
						value: String(row.instalment_number),
						label: __("#{0} due {1} — {2}", [
							row.instalment_number,
							frappe.datetime.str_to_user(row.due_date),
							format_currency(row.instalment_amount),
						]),
					})),
				},
				{ fieldtype: "Column Break" },
				{
					fieldname: "paid_date",
					fieldtype: "Date",
					label: __("Paid Date"),
					reqd: 1,
					default: frappe.datetime.get_today(),
				},
				{ fieldtype: "Section Break" },
				{
					fieldname: "paid_amount",
					fieldtype: "Currency",
					label: __("Paid Amount"),
					reqd: 1,
					default: next.instalment_amount,
					description: __("Less than the instalment marks it Partially Paid."),
				},
				{ fieldtype: "Column Break" },
				{
					fieldname: "payment_entry",
					fieldtype: "Link",
					label: __("Journal Entry"),
					options: "Journal Entry",
					get_query: () => ({ filters: { company: frm.doc.company, docstatus: 1 } }),
				},
				{ fieldtype: "Section Break" },
				{ fieldname: "remarks", fieldtype: "Small Text", label: __("Remarks") },
			],
			primary_action_label: __("Mark Paid"),
			primary_action(values) {
				frm.call({
					method: "mark_instalment_paid",
					doc: frm.doc,
					args: values,
					freeze: true,
				}).then((r) => {
					dialog.hide();
					frm.reload_doc();
					if (r.message) {
						frappe.show_alert({
							message: __("Instalment {0} marked {1}.", [
								values.instalment_number,
								__(r.message),
							]),
							indicator: "green",
						});
					}
				});
			},
		});

		// Default the amount to whichever instalment is picked.
		dialog.fields_dict.instalment_number.$input.on("change", () => {
			const picked = unpaid.find(
				(row) => String(row.instalment_number) === dialog.get_value("instalment_number")
			);
			if (picked) dialog.set_value("paid_amount", picked.instalment_amount);
		});

		dialog.show();
	},

	recalculate_from(frm) {
		const open = (frm.doc.repayment_schedule || []).filter((row) =>
			["Unpaid", "Overdue"].includes(row.payment_status)
		);

		if (!open.length) {
			frappe.msgprint({
				title: __("Nothing to Recalculate"),
				message: __("Every instalment is settled, so there is no tail to regenerate."),
				indicator: "orange",
			});
			return;
		}

		const dialog = new frappe.ui.Dialog({
			title: __("Recalculate from Instalment"),
			fields: [
				{
					fieldname: "instalment_number",
					fieldtype: "Select",
					label: __("Recalculate From"),
					reqd: 1,
					default: String(open[0].instalment_number),
					options: open.map((row) => ({
						value: String(row.instalment_number),
						label: __("#{0} due {1}", [
							row.instalment_number,
							frappe.datetime.str_to_user(row.due_date),
						]),
					})),
					description: __("Everything before this instalment is left untouched."),
				},
				{ fieldtype: "Section Break" },
				{
					fieldname: "rate_of_interest",
					fieldtype: "Percent",
					label: __("Revised Rate of Interest"),
					description: __("Leave blank to keep {0}%.", [frm.doc.rate_of_interest]),
				},
				{ fieldtype: "Column Break" },
				{
					fieldname: "outstanding_principal",
					fieldtype: "Currency",
					label: __("Revised Opening Principal"),
					description: __("Set this after a part-prepayment. Blank keeps the current balance."),
				},
			],
			primary_action_label: __("Recalculate"),
			primary_action(values) {
				frm.call({
					method: "recalculate_from",
					doc: frm.doc,
					args: values,
					freeze: true,
					freeze_message: __("Regenerating the unpaid instalments..."),
				}).then((r) => {
					dialog.hide();
					frm.reload_doc();
					if (r.message) {
						frappe.show_alert({
							message: __("Recalculated {0} instalments.", [r.message]),
							indicator: "green",
						});
					}
				});
			},
		});

		dialog.show();
	},
});
