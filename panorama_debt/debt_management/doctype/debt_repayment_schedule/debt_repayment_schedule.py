# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from panorama_debt.debt_management.schedule_engine import (
	INTEREST_CAPITALISED,
	MANUAL,
	MONTHS_PER_PERIOD,
	generate_schedule,
	get_moratorium_periods,
	money,
	summarise_schedule,
)

# Schedules must reconcile to the facility to the paisa; this is the slack
# allowed when comparing two already-rounded money figures.
RECONCILIATION_TOLERANCE = 0.01

# Fields copied from the facility onto the schedule before generating, so the
# engine is driven by one consistent set of terms.
FACILITY_TERM_FIELDS = (
	"rate_of_interest",
	"number_of_instalments",
	"moratorium_months",
	"moratorium_type",
	"repayment_method",
	"repayment_frequency",
	"repayment_start_date",
	"disbursed_amount",
)


class DebtRepaymentSchedule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from panorama_debt.debt_management.doctype.debt_repayment_schedule_detail.debt_repayment_schedule_detail import (
			DebtRepaymentScheduleDetail,
		)

		amended_from: DF.Link | None
		company: DF.Link
		disbursed_amount: DF.Currency
		is_active: DF.Check
		loan: DF.Link
		loan_account_number: DF.Data | None
		loan_provider: DF.Link
		loan_type: DF.Data | None
		moratorium_months: DF.Int
		moratorium_type: DF.Data | None
		naming_series: DF.Literal["LRS-.YYYY.-"]
		number_of_instalments: DF.Int
		outstanding_principal: DF.Currency
		posting_date: DF.Date
		rate_of_interest: DF.Percent
		repayment_frequency: DF.Data | None
		repayment_method: DF.Data | None
		repayment_schedule: DF.Table[DebtRepaymentScheduleDetail]
		repayment_start_date: DF.Date | None
		sanctioned_amount: DF.Currency
		schedule_source: DF.Literal["System Generated", "Imported from Sanction Letter"]
		status: DF.Literal["Draft", "Active", "Revised", "Closed", "Cancelled"]
		tenure_months: DF.Int
		total_interest: DF.Currency
		total_interest_paid: DF.Currency
		total_payable: DF.Currency
		total_principal: DF.Currency
		total_principal_paid: DF.Currency
	# end: auto-generated types

	def validate(self):
		self.validate_facility()
		self.renumber_instalments()
		self.calculate_totals()
		self.set_status()

	def before_submit(self):
		self.validate_schedule_not_empty()
		self.validate_schedule_reconciles()

	def on_submit(self):
		"""Make this the facility's live schedule and push the numbers onto it."""
		self.supersede_previous_schedules()
		self.update_facility()

	def on_cancel(self):
		"""Stand the schedule down without silently promoting another one.

		The previous schedule is deliberately NOT reactivated: which schedule
		should be live after a cancellation is a business decision, and
		guessing would quietly resurrect terms nobody re-checked.
		"""
		self.db_set("status", "Cancelled")
		self.db_set("is_active", 0)
		self.update_facility(clear=True)

	# --- validation ---------------------------------------------------------

	def validate_facility(self):
		"""The schedule must belong to the same facility it claims to schedule.

		Company and provider are on this document so the user can narrow the
		facility picker, which makes them easy to leave pointing somewhere
		else; they are re-checked here against the facility itself.
		"""
		facility = frappe.db.get_value(
			"Debt Facility",
			self.loan,
			["company", "loan_provider", "is_revolving", "docstatus"],
			as_dict=True,
		)

		if not facility:
			frappe.throw(_("Debt Facility {0} not found.").format(frappe.bold(self.loan)))

		if facility.docstatus != 1:
			frappe.throw(
				_("Debt Facility {0} is not submitted. Submit the facility before scheduling it.").format(
					frappe.bold(self.loan)
				),
				title=_("Facility Not Submitted"),
			)

		if facility.is_revolving:
			frappe.throw(
				_("{0} is a revolving facility and carries no repayment schedule.").format(
					frappe.bold(self.loan)
				),
				title=_("Revolving Facility"),
			)

		for fieldname, expected in (("company", facility.company), ("loan_provider", facility.loan_provider)):
			if self.get(fieldname) != expected:
				frappe.throw(
					_("{0} does not match the facility, which belongs to {1}.").format(
						_(self.meta.get_label(fieldname)), frappe.bold(expected)
					),
					title=_("Facility Mismatch"),
				)

	def renumber_instalments(self):
		"""Keep instalment numbers contiguous after a manual insert or delete.

		Rows imported from a sanction letter get keyed in by hand and rows get
		deleted, so the numbering is rewritten from the row order rather than
		trusted.
		"""
		for index, row in enumerate(self.repayment_schedule, start=1):
			row.instalment_number = index

	def validate_schedule_not_empty(self):
		if not self.repayment_schedule:
			frappe.throw(
				_("The repayment schedule is empty. Generate it or import it before submitting."),
				title=_("Nothing to Submit"),
			)

	def validate_schedule_reconciles(self):
		"""Check the schedule ties back to the facility and repays it in full.

		Three ties, in the order they usually break:

		1. The opening balance of the first instalment is what was disbursed.
		2. The principal repaid across all instalments equals that same figure
		   -- except under a capitalising moratorium, where accrued interest
		   becomes principal and the grossed-up balance is what gets repaid.
		3. The final instalment leaves nothing outstanding.
		"""
		disbursed = money(self.disbursed_amount)
		opening = money(self.repayment_schedule[0].outstanding_principal)

		if abs(float(opening - disbursed)) > RECONCILIATION_TOLERANCE:
			frappe.throw(
				_("The first instalment opens at {0} but {1} was disbursed.").format(
					frappe.bold(self.fmt(opening)), frappe.bold(self.fmt(disbursed))
				),
				title=_("Schedule Does Not Match Disbursement"),
			)

		expected = self.expected_principal()
		repaid = money(self.total_principal)

		if abs(float(repaid - expected)) > RECONCILIATION_TOLERANCE:
			frappe.throw(
				_("The schedule repays {0} of principal but {1} has to be repaid.").format(
					frappe.bold(self.fmt(repaid)), frappe.bold(self.fmt(expected))
				),
				title=_("Principal Does Not Reconcile"),
			)

		closing = money(self.repayment_schedule[-1].closing_principal)
		if closing != 0:
			frappe.throw(
				_("The final instalment leaves {0} outstanding. A schedule has to close at zero.").format(
					frappe.bold(self.fmt(closing))
				),
				title=_("Schedule Does Not Close"),
			)

	def expected_principal(self):
		"""Principal the schedule has to repay, grossed up for capitalisation.

		Under a full moratorium the accrued interest is added to the balance
		instead of being billed, so more principal is repaid than was ever
		disbursed. The grossed-up figure is read off the schedule itself --
		the opening balance of the first instalment that actually repays
		principal -- rather than recomputed, so it cannot drift from the rows.
		"""
		if self.moratorium_type != INTEREST_CAPITALISED:
			return money(self.disbursed_amount)

		for row in self.repayment_schedule:
			if flt(row.principal_amount):
				return money(row.outstanding_principal)

		return money(self.disbursed_amount)

	# --- numbers ------------------------------------------------------------

	def calculate_totals(self):
		"""Roll the rows up into the document totals.

		Everything is summed from the rounded row figures rather than
		recomputed from the facility terms, so the totals always agree with
		what is on screen to the paisa.
		"""
		rows = [row.as_dict() for row in self.repayment_schedule]
		totals = summarise_schedule(rows)

		self.total_principal = totals["total_principal"]
		self.total_interest = totals["total_interest"]
		self.total_payable = totals["total_payable"]

		self.total_principal_paid = float(
			sum((money(r.paid_principal()) for r in self.repayment_schedule), money(0))
		)
		self.total_interest_paid = float(
			sum((money(r.paid_interest()) for r in self.repayment_schedule), money(0))
		)
		self.outstanding_principal = float(money(self.total_principal) - money(self.total_principal_paid))

	def set_status(self):
		if self.docstatus == 0:
			self.status = "Draft"
		elif self.docstatus == 1 and self.status in (None, "", "Draft"):
			self.status = "Active"
			self.is_active = 1

	# --- generation ---------------------------------------------------------

	@frappe.whitelist()
	def generate_schedule(self):
		"""Fill the child table from the facility's terms using the engine.

		Every existing row is replaced. The caller is responsible for warning
		the user first -- the client script does, and any paid row would be
		lost here, which is why "Recalculate from Instalment #" exists as the
		separate, non-destructive path for a live schedule.

		Returns the number of rows generated so the caller can report it. A
		facility on "As per Bank Schedule (Manual)" generates nothing by
		design; its rows come from the bank's own annexure.
		"""
		if self.docstatus != 0:
			frappe.throw(_("Only a draft schedule can be generated."), title=_("Not a Draft"))

		self.pull_facility_terms()

		if self.repayment_method == MANUAL:
			frappe.msgprint(
				_(
					"{0} is set to {1}, so no schedule is generated. Key the rows in from the bank's annexure."
				).format(frappe.bold(self.loan), frappe.bold(_(MANUAL))),
				title=_("Manual Schedule"),
				indicator="orange",
			)
			return 0

		if flt(self.disbursed_amount) <= 0:
			frappe.throw(
				_("{0} has nothing disbursed yet, so there is nothing to amortise.").format(
					frappe.bold(self.loan)
				),
				title=_("Nothing Disbursed"),
			)

		rows = generate_schedule(
			principal=self.disbursed_amount,
			rate_of_interest=self.rate_of_interest,
			number_of_instalments=self.number_of_instalments,
			repayment_method=self.repayment_method,
			repayment_frequency=self.repayment_frequency or "Monthly",
			repayment_start_date=self.repayment_start_date,
			moratorium_months=self.moratorium_months,
			moratorium_type=self.moratorium_type,
		)

		self.set("repayment_schedule", [])
		for row in rows:
			self.append("repayment_schedule", row)

		self.schedule_source = "System Generated"
		self.calculate_totals()
		return len(rows)

	@frappe.whitelist()
	def clear_schedule(self):
		"""Empty the child table and reset the totals with it."""
		if self.docstatus != 0:
			frappe.throw(_("Only a draft schedule can be cleared."), title=_("Not a Draft"))

		self.set("repayment_schedule", [])
		self.calculate_totals()
		return 0

	def pull_facility_terms(self):
		"""Refresh the facility's terms onto this document before generating.

		fetch_from only fires when the link changes in the UI, so a facility
		edited after the schedule was created would otherwise be amortised on
		stale terms.
		"""
		facility = frappe.get_doc("Debt Facility", self.loan)
		for fieldname in FACILITY_TERM_FIELDS:
			self.set(fieldname, facility.get(fieldname))

	# --- facility upkeep ----------------------------------------------------

	def supersede_previous_schedules(self):
		"""Stand down any schedule already active for this facility.

		A facility carries exactly one live schedule. Rate revisions and
		restructures arrive as a fresh schedule, and the one it replaces is
		kept for audit rather than deleted.
		"""
		superseded = frappe.get_all(
			"Debt Repayment Schedule",
			filters={"loan": self.loan, "is_active": 1, "docstatus": 1, "name": ("!=", self.name)},
			pluck="name",
		)

		for name in superseded:
			schedule = frappe.get_doc("Debt Repayment Schedule", name)
			schedule.db_set("is_active", 0)
			schedule.db_set("status", "Revised")

		if superseded:
			frappe.msgprint(
				_("Marked {0} as Revised.").format(frappe.bold(", ".join(superseded))),
				alert=True,
			)

	def update_facility(self, clear=False):
		"""Push this schedule's position onto the facility it belongs to.

		Written with db_set because the facility is submitted and its status
		fields are read-only. Passing clear=True zeroes them, which is what a
		cancellation needs.
		"""
		facility = frappe.get_doc("Debt Facility", self.loan)

		if clear:
			values = dict.fromkeys(
				(
					"total_payable",
					"total_interest_payable",
					"outstanding_principal",
					"total_principal_paid",
					"total_interest_paid",
					"next_due_amount",
				),
				0,
			)
			values["next_due_date"] = None
		else:
			next_due = self.get_next_due_instalment()
			values = {
				"total_payable": flt(self.total_payable),
				"total_interest_payable": flt(self.total_interest),
				"outstanding_principal": flt(self.outstanding_principal),
				"total_principal_paid": flt(self.total_principal_paid),
				"total_interest_paid": flt(self.total_interest_paid),
				"next_due_date": next_due.due_date if next_due else None,
				"next_due_amount": flt(next_due.instalment_amount) if next_due else 0,
			}

		for fieldname, value in values.items():
			facility.db_set(fieldname, value)

	def get_next_due_instalment(self):
		"""Earliest instalment still owing, by due date.

		Waived rows are skipped -- they are settled, just not with money --
		and partially paid rows still count as due for the balance.
		"""
		outstanding = [
			row
			for row in self.repayment_schedule
			if row.payment_status not in ("Paid", "Waived") and row.due_date
		]
		return min(outstanding, key=lambda row: getdate(row.due_date), default=None)

	# --- payments and revisions ---------------------------------------------

	@frappe.whitelist()
	def mark_instalment_paid(
		self, instalment_number, paid_date, paid_amount, payment_entry=None, remarks=None
	):
		"""Settle one instalment and roll the change through to the facility.

		A receipt short of the instalment leaves the row Partially Paid; the
		shortfall keeps the row in the running for next-due. Overpayment is
		refused rather than absorbed: money beyond the instalment is a
		prepayment, which changes the rest of the schedule and belongs in
		Recalculate from Instalment, not in a single row.
		"""
		row = self.get_instalment(instalment_number)

		if row.payment_status in ("Paid", "Waived"):
			frappe.throw(
				_("Instalment {0} is already {1}.").format(row.instalment_number, _(row.payment_status)),
				title=_("Already Settled"),
			)

		paid_amount = flt(paid_amount)
		due = flt(row.instalment_amount)

		if paid_amount <= 0:
			frappe.throw(_("Paid Amount must be greater than zero."), title=_("Invalid Payment"))

		if paid_amount - due > RECONCILIATION_TOLERANCE:
			frappe.throw(
				_(
					"{0} is more than instalment {1} of {2}. Record the excess as a prepayment instead."
				).format(
					frappe.bold(self.fmt(paid_amount)),
					row.instalment_number,
					frappe.bold(self.fmt(due)),
				),
				title=_("Overpayment"),
			)

		settled = abs(paid_amount - due) <= RECONCILIATION_TOLERANCE
		values = {
			"payment_status": "Paid" if settled else "Partially Paid",
			"paid_date": getdate(paid_date),
			"paid_amount": paid_amount,
			"payment_entry": payment_entry,
			"remarks": remarks,
		}
		self.apply_row_values(row, values)

		self.reload()
		self.refresh_totals()
		self.update_facility()
		return values["payment_status"]

	@frappe.whitelist()
	def recalculate_from(self, instalment_number, rate_of_interest=None, outstanding_principal=None):
		"""Regenerate the unpaid tail after a rate revision or a part-prepayment.

		Everything before the chosen instalment is frozen exactly as it stands,
		including its paid rows. The tail keeps its instalment count and its
		due dates, so only the money moves -- a revision re-prices the loan, it
		does not re-length it. Any moratorium still ahead of the chosen point
		is carried into the regeneration rather than silently dropped.

		Leave either override blank to keep the facility's current rate and the
		balance the schedule already shows at that point.
		"""
		index = self.get_instalment_index(instalment_number)
		tail = self.repayment_schedule[index:]

		settled = [
			r.instalment_number for r in tail if r.payment_status in ("Paid", "Partially Paid", "Waived")
		]
		if settled:
			frappe.throw(
				_(
					"Instalment {0} onwards includes settled instalments ({1}). Recalculate from a later instalment."
				).format(instalment_number, ", ".join(str(n) for n in settled)),
				title=_("Settled Instalments in Range"),
			)

		if self.repayment_method == MANUAL:
			frappe.throw(
				_(
					"This facility uses {0}, so its rows are keyed in by hand and cannot be recalculated."
				).format(frappe.bold(_(MANUAL))),
				title=_("Manual Schedule"),
			)

		opening = flt(outstanding_principal) if outstanding_principal else flt(tail[0].outstanding_principal)
		rate = flt(rate_of_interest) if rate_of_interest not in (None, "") else flt(self.rate_of_interest)
		frequency = self.repayment_frequency or "Monthly"

		rows = generate_schedule(
			principal=opening,
			rate_of_interest=rate,
			number_of_instalments=len(tail),
			repayment_method=self.repayment_method,
			repayment_frequency=frequency,
			repayment_start_date=tail[0].due_date,
			moratorium_months=self.remaining_moratorium_months(index),
			moratorium_type=self.moratorium_type,
		)

		for row, values in zip(tail, rows, strict=True):
			self.apply_row_values(
				row,
				{
					key: values[key]
					for key in (
						"outstanding_principal",
						"principal_amount",
						"interest_amount",
						"instalment_amount",
						"closing_principal",
						"due_date",
					)
				},
			)

		self.reload()
		self.refresh_totals()
		self.update_facility()
		return len(tail)

	def remaining_moratorium_months(self, index):
		"""Moratorium still ahead of a recalculation point, back in months.

		Converted to periods to subtract the instalments already behind us,
		then back to months because that is what the engine takes.
		"""
		frequency = self.repayment_frequency or "Monthly"
		remaining = max(0, get_moratorium_periods(self.moratorium_months, frequency) - index)
		return remaining * MONTHS_PER_PERIOD[frequency]

	def get_instalment_index(self, instalment_number):
		"""Row position of an instalment number, or a clear error."""
		index = next(
			(
				i
				for i, row in enumerate(self.repayment_schedule)
				if row.instalment_number == int(instalment_number)
			),
			None,
		)
		if index is None:
			frappe.throw(
				_("Instalment {0} is not in this schedule.").format(instalment_number),
				title=_("No Such Instalment"),
			)
		return index

	def get_instalment(self, instalment_number):
		return self.repayment_schedule[self.get_instalment_index(instalment_number)]

	def apply_row_values(self, row, values):
		"""Write row values the way the document state allows.

		A submitted schedule goes through db_set, which writes past the
		read-only guard on fields that were never meant to be typed into.
		"""
		if self.docstatus == 1:
			for fieldname, value in values.items():
				row.db_set(fieldname, value)
		else:
			row.update(values)

	# Every total calculate_totals() derives. All of them have to be persisted
	# after a recalculation: a rate revision changes what is payable, not just
	# what has been paid.
	TOTAL_FIELDS = (
		"total_principal",
		"total_interest",
		"total_payable",
		"total_principal_paid",
		"total_interest_paid",
		"outstanding_principal",
	)

	def refresh_totals(self):
		"""Recompute the totals and persist them on an already-submitted schedule.

		db_set is used because these fields are read-only and the document is
		past submission; it also skips the update-after-submit guard, which is
		the point -- the numbers are derived, never typed.
		"""
		self.calculate_totals()
		for fieldname in self.TOTAL_FIELDS:
			self.db_set(fieldname, flt(self.get(fieldname)))

	def fmt(self, value):
		"""Format an amount in the facility's company currency for a message."""
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		return frappe.utils.fmt_money(flt(value), currency=currency)
