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
		frm.trigger("add_upload_buttons");
	},

	add_upload_buttons(frm) {
		frm.add_custom_button(
			__("Download Template"),
			() => {
				// Pre-filled from the saved document, so a round trip edits
				// what is already there instead of starting from nothing.
				const params = frm.is_new() ? "" : `?schedule=${encodeURIComponent(frm.doc.name)}`;
				window.open(
					`/api/method/panorama_debt.debt_management.doctype.debt_repayment_schedule.debt_repayment_schedule.download_schedule_template${params}`
				);
			},
			__("Upload")
		);

		if (frm.doc.docstatus !== 0) return;

		frm.add_custom_button(
			__("Upload Schedule"),
			() => {
				if (frm.is_new()) {
					frappe.msgprint({
						title: __("Save First"),
						message: __("Save the draft before uploading, so the file can be attached to it."),
						indicator: "orange",
					});
					return;
				}

				const upload = () =>
					new frappe.ui.FileUploader({
						doctype: frm.doctype,
						docname: frm.doc.name,
						// Keep the source file on the document: an imported
						// schedule should always be traceable to the annexure
						// it came from.
						folder: "Home/Attachments",
						restrictions: { allowed_file_types: [".xlsx", ".xlsm", ".csv"] },
						upload_notes: __("Columns: Due Date, Principal Amount, Interest, Status, Payment Entry Reference."),
						on_success: (file) => {
							frm.call({
								method: "import_schedule",
								doc: frm.doc,
								args: { file_url: file.file_url },
								freeze: true,
								freeze_message: __("Reading the schedule..."),
							}).then((r) => {
								frm.refresh_field("repayment_schedule");
								frm.dirty();
								if (r.message) {
									frappe.show_alert({
										message: __("Imported {0} instalments. Review them, then save.", [
											r.message,
										]),
										indicator: "green",
									});
								}
							});
						},
					});

				if ((frm.doc.repayment_schedule || []).length) {
					frappe.confirm(
						__("This replaces all {0} rows in the schedule. Continue?", [
							frm.doc.repayment_schedule.length,
						]),
						upload
					);
				} else {
					upload();
				}
			},
			__("Upload")
		);
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
		// Payments belong to the facility's live schedule only. A superseded
		// revision or a cancelled document still renders, so the buttons are
		// hidden rather than left to fail server-side.
		if (frm.doc.docstatus !== 1 || !frm.doc.is_active || frm.doc.status !== "Active") return;

		frm.add_custom_button(__("Make Payment Entry"), () =>
			frm.trigger("make_payment_entry")
		).addClass("btn-primary");

		frm.add_custom_button(__("Mark Paid (Entry Already Posted)"), () =>
			frm.trigger("mark_instalment_paid")
		);

		frm.add_custom_button(__("Reverse Payment"), () => frm.trigger("reverse_payment"));

		frm.add_custom_button(__("Recalculate from Instalment"), () =>
			frm.trigger("recalculate_from")
		);
	},

	make_payment_entry(frm) {
		const today = frappe.datetime.get_today();
		const open = (frm.doc.repayment_schedule || []).filter((row) =>
			["Unpaid", "Overdue"].includes(row.payment_status)
		);
		// An instalment cannot be settled before it falls due, so offering one
		// would only produce a server-side rejection.
		const due = open.filter((row) => row.due_date <= today);

		if (!due.length) {
			const next = open[0];
			frappe.msgprint({
				title: __("Nothing Due Yet"),
				message: next
					? __("The next instalment, #{0}, falls due on {1}. It cannot be paid before the due date.", [
							next.instalment_number,
							frappe.datetime.str_to_user(next.due_date),
					  ])
					: __("Every instalment on this schedule is already settled."),
				indicator: "orange",
			});
			return;
		}

		// One round trip for all four ledgers, so the dialog can show what the
		// entry will actually post against without querying per field.
		frappe.db
			.get_value("Debt Facility", frm.doc.loan, [
				"loan_liability_account",
				"interest_expense_account",
				"bank_account",
				"repayment_bank_account",
			])
			.then((r) => frm.events.show_payment_dialog(frm, due, r.message || {}));
	},

	show_payment_dialog(frm, due, accounts) {
		const first = due[0];
		const NOT_SET = __("Not set on the facility");
		const is_capitalised = (row) => !flt(row.instalment_amount) && flt(row.interest_amount);
		const pick = () =>
			due.find((r) => String(r.instalment_number) === dialog.get_value("instalment_number")) || first;
		const ledger_filter = (root_type) => ({
			filters: { company: frm.doc.company, is_group: 0, root_type },
		});
		const bank_filter = () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
				account_type: ["in", ["Bank", "Cash"]],
			},
		});

		const dialog = new frappe.ui.Dialog({
			title: __("Make Payment Entry"),
			size: "large",
			fields: [
				// Alone at full width: the label carries the due date and amount
				// and gets truncated in a half-width control.
				{
					fieldname: "instalment_number",
					fieldtype: "Select",
					label: __("Instalment"),
					reqd: 1,
					default: String(first.instalment_number),
					options: due.map((row) => ({
						value: String(row.instalment_number),
						label: __("#{0} due {1} — {2}", [
							row.instalment_number,
							frappe.datetime.str_to_user(row.due_date),
							format_currency(row.instalment_amount),
						]),
					})),
				},
				{ fieldtype: "Section Break" },
				{
					fieldname: "posting_date",
					fieldtype: "Date",
					label: __("Posting Date"),
					reqd: 1,
					default: first.due_date,
					description: __("On or after the due date, and not in the future."),
				},
				{ fieldtype: "Column Break" },
				{
					fieldname: "reference_no",
					fieldtype: "Data",
					label: __("Reference No / UTR"),
					description: __("Leave blank to reference this schedule and instalment."),
				},
				{ fieldtype: "Section Break", label: __("Accounts (from Debt Facility)") },
				{
					// Loan, Interest and the Payable bank are editable: a
					// facility can be reclassified, or one month's interest
					// booked to a different head. Each override is validated
					// server-side exactly as the facility's own field would be.
					fieldname: "loan_account",
					fieldtype: "Link",
					label: __("Loan Account"),
					options: "Account",
					default: accounts.loan_liability_account || "",
					get_query: () => ledger_filter("Liability"),
					description: __("Debited for the principal."),
				},
				{
					fieldname: "interest_account",
					fieldtype: "Link",
					label: __("Interest Account"),
					options: "Account",
					default: accounts.interest_expense_account || "",
					get_query: () => ledger_filter("Expense"),
					description: __("Debited for the interest and charges."),
				},
				{ fieldtype: "Column Break" },
				{
					// Read-only on purpose. A repayment has no receipt leg, so
					// an editable control here would do nothing -- it is shown
					// only so the two bank accounts can be told apart at a
					// glance. Change it on the Debt Facility itself.
					fieldname: "receipt_bank_account",
					fieldtype: "Link",
					label: __("Bank Account (Receipt of Loan Amount)"),
					options: "Account",
					read_only: 1,
					default: accounts.bank_account || "",
					description: __("For reference; not used in this entry."),
				},
				{
					fieldname: "bank_account",
					fieldtype: "Link",
					label: __("Bank Account (Repayment of Loan)"),
					options: "Account",
					reqd: 1,
					default: accounts.repayment_bank_account || "",
					get_query: () => bank_filter(),
					description: __("Credited for the whole instalment."),
				},
				{ fieldtype: "Section Break" },
				{ fieldname: "preview", fieldtype: "HTML", label: __("Entry") },
			],
			primary_action_label: __("Post Entry"),
			primary_action(values) {
				frm.call({
					method: "make_payment_entry",
					doc: frm.doc,
					args: {
						instalment_number: values.instalment_number,
						posting_date: values.posting_date,
						bank_account: values.bank_account,
						reference_no: values.reference_no,
						loan_account: values.loan_account,
						interest_account: values.interest_account,
					},
					freeze: true,
					freeze_message: __("Posting the repayment..."),
				}).then((r) => {
					dialog.hide();
					frm.reload_doc();
					if (r.message) {
						frappe.show_alert({
							message: __("Posted {0}.", [
								frappe.utils.get_form_link("Journal Entry", r.message, true),
							]),
							indicator: "green",
						});
					}
				});
			},
		});

		const render = () => {
			const row = pick();
			const capitalised = is_capitalised(row);
			const interest = flt(row.interest_amount) + flt(row.other_charges);
			const needs_loan = capitalised || flt(row.principal_amount) > 0;
			const needs_interest = interest > 0;

			// Only complain about an account this particular instalment needs:
			// an interest-free loan legitimately has no interest ledger.
			const loan = dialog.get_value("loan_account");
			const interest_account = dialog.get_value("interest_account");
			const bank = dialog.get_value("bank_account") || NOT_SET;
			const shown = (value, needed) => value || (needed ? NOT_SET : "—");
			const lines = capitalised
				? [
						[__("Interest Account"), shown(interest_account, needs_interest), format_currency(interest), ""],
						[__("Loan Account"), shown(loan, needs_loan), "", format_currency(interest)],
				  ]
				: [
						[__("Loan Account"), shown(loan, needs_loan), format_currency(row.principal_amount), ""],
						[__("Interest Account"), shown(interest_account, needs_interest), format_currency(interest), ""],
						[__("Bank"), bank, "", format_currency(row.instalment_amount)],
				  ];

			const body = lines
				.filter(([, , dr, cr]) => dr || cr)
				.map(
					([label, account, dr, cr]) =>
						`<tr><td>${frappe.utils.escape_html(label)}<br>
						<span class="text-muted small">${frappe.utils.escape_html(account || "")}</span></td>
						<td class="text-right">${dr}</td><td class="text-right">${cr}</td></tr>`
				)
				.join("");

			dialog.fields_dict.preview.$wrapper.html(`
				${
					capitalised
						? `<p class="text-muted small">${__(
								"This instalment falls in a capitalising moratorium. No money moves and no bank account is involved: the interest is added to the loan balance."
						  )}</p>`
						: ""
				}
				<div style="overflow-x: auto;">
				<table class="table table-bordered small">
					<thead><tr><th>${__("Account")}</th><th class="text-right">${__("Debit")}</th>
					<th class="text-right">${__("Credit")}</th></tr></thead>
					<tbody>${body}</tbody>
				</table></div>`);
			dialog.set_value("posting_date", row.due_date);
		};

		dialog.fields_dict.instalment_number.$input.on("change", render);
		["bank_account", "loan_account", "interest_account"].forEach(
			(field) => (dialog.fields_dict[field].df.onchange = render)
		);
		dialog.show();
		render();
	},

	reverse_payment(frm) {
		const settled = (frm.doc.repayment_schedule || []).filter((row) =>
			["Paid", "Partially Paid"].includes(row.payment_status)
		);

		if (!settled.length) {
			frappe.msgprint({
				title: __("Nothing to Reverse"),
				message: __("No instalment on this schedule is settled."),
				indicator: "orange",
			});
			return;
		}

		const dialog = new frappe.ui.Dialog({
			title: __("Reverse Payment"),
			size: "large",
			fields: [
				{
					fieldname: "instalment_number",
					fieldtype: "Select",
					label: __("Instalment"),
					reqd: 1,
					default: String(settled[settled.length - 1].instalment_number),
					options: settled.map((row) => ({
						value: String(row.instalment_number),
						label: __("#{0} due {1} — {2}", [
							row.instalment_number,
							frappe.datetime.str_to_user(row.due_date),
							__(row.payment_status),
						]),
					})),
				},
				{ fieldtype: "Section Break" },
				{ fieldname: "effect", fieldtype: "HTML" },
			],
			primary_action_label: __("Reverse"),
			primary_action(values) {
				frm.call({
					method: "reverse_payment",
					doc: frm.doc,
					args: values,
					freeze: true,
				}).then((r) => {
					dialog.hide();
					frm.reload_doc();
					frappe.show_alert({
						message: r.message
							? __("Reversed. Journal Entry {0} cancelled.", [r.message])
							: __("Instalment reset. No Journal Entry was cancelled."),
						indicator: "orange",
					});
				});
			},
		});

		// Say up front whether a ledger entry goes with the reset, because
		// only entries this app posted are ever cancelled.
		const render_effect = () => {
			const row = settled.find(
				(r) => String(r.instalment_number) === dialog.get_value("instalment_number")
			);
			if (!row) return;
			dialog.fields_dict.effect.$wrapper.html(
				row.entry_posted_by_system && row.payment_entry
					? `<p class="text-danger">${__("Journal Entry {0} will be cancelled.", [
							row.payment_entry,
					  ])}</p>`
					: `<p class="text-muted">${__(
							"Only the instalment is reset. No Journal Entry will be cancelled, because this app did not post one for it."
					  )}</p>`
			);
		};
		dialog.fields_dict.instalment_number.$input.on("change", render_effect);
		dialog.show();
		render_effect();
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
			size: "large",
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
				{ fieldtype: "Section Break" },
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
			size: "large",
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
