# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.desk.utils import provide_binary_file
from frappe.model.document import Document
from frappe.utils import escape_html, flt, formatdate, getdate, nowdate
from frappe.utils.csvutils import read_csv_content
from frappe.utils.xlsxutils import make_xlsx, read_xlsx_file_from_attached_file

from panorama_debt.debt_management.accounting import (
	cancel_journal_entries,
	journal_entry_reference_error,
	make_journal_entry,
	validate_settlement_date,
)
from panorama_debt.debt_management.doctype.debt_facility.debt_facility import (
	validate_bank_account,
	validate_ledger_account,
)
from panorama_debt.debt_management.schedule_engine import (
	INTEREST_CAPITALISED,
	MANUAL,
	MONTHS_PER_PERIOD,
	ZERO,
	derive_balances,
	generate_schedule,
	get_moratorium_periods,
	money,
	summarise_schedule,
)
from panorama_debt.debt_management.schedule_import import parse_schedule_rows

# Schedule sources whose balances this app derives rather than trusts. The bank
# supplies principal and interest on these; the ledger columns follow.
DERIVED_BALANCE_SOURCES = ("Imported from Sanction Letter",)

# Rows a payment may still be posted against.
PAYABLE_STATUSES = ("Unpaid", "Overdue")

# Ledgers a repayment may be paid out of.
BANK_ACCOUNT_TYPES = ("Bank", "Cash")

# The upload template, in the order the columns appear.
TEMPLATE_COLUMNS = (
	("due_date", "Due Date"),
	("principal_amount", "Principal Amount"),
	("interest_amount", "Interest"),
	("payment_status", "Status"),
	("payment_entry", "Payment Entry Reference"),
)

# Beyond this many problems the message becomes unreadable; the rest are
# counted instead of listed.
MAX_REPORTED_ERRORS = 25

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
		self.validate_due_dates_increase()
		self.renumber_instalments()
		self.apply_derived_balances()
		self.calculate_totals()
		self.set_status()

	def before_submit(self):
		self.validate_schedule_not_empty()
		self.validate_schedule_reconciles()
		self.validate_settled_instalments_carried_over()

	def before_cancel(self):
		"""Refuse to cancel a superseded schedule that still owns live postings.

		A Revised schedule is no longer the facility's record of what is owed,
		but its system-posted entries are still the ledger's record of what was
		actually paid. Cancelling it would reverse real repayments that the
		live schedule believes have happened. Cancel the live schedule first,
		or reverse the individual payments, so the reversal is deliberate.
		"""
		if self.status != "Revised":
			return

		live = [
			row.payment_entry
			for row in self.repayment_schedule
			if row.entry_posted_by_system
			and row.payment_entry
			and frappe.db.get_value("Journal Entry", row.payment_entry, "docstatus") == 1
		]

		if live:
			frappe.throw(
				_(
					"This schedule has been revised but still owns {0} submitted payment entries ({1}). Reverse those payments before cancelling it."
				).format(len(live), frappe.bold(", ".join(live[:5]))),
				title=_("Revised Schedule Has Live Entries"),
			)

	def on_submit(self):
		"""Make this the facility's live schedule and push the numbers onto it."""
		self.supersede_previous_schedules()
		self.update_facility()

	def on_cancel(self):
		"""Stand the schedule down, reversing only the postings it made itself.

		The previous schedule is deliberately NOT reactivated: which schedule
		should be live after a cancellation is a business decision, and
		guessing would quietly resurrect terms nobody re-checked.

		Entries a user recorded by hand are left alone. This app did not post
		them, does not know what else they cover, and must not reverse
		somebody else's journal.

		The facility is only reset if this schedule was the live one. A
		superseded schedule cancelling itself must not wipe figures that now
		belong to its replacement.
		"""
		was_active = bool(self.is_active)

		cancel_journal_entries(
			[row.payment_entry for row in self.repayment_schedule if row.entry_posted_by_system]
		)

		self.db_set("status", "Cancelled")
		self.db_set("is_active", 0)

		if was_active:
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

	def validate_due_dates_increase(self):
		"""Instalments must run forwards in time, however the rows got here.

		The upload path already refuses an out-of-order file, but rows can also
		be typed, pasted or dragged in the grid. Every balance in the schedule
		is derived by walking the rows in order, so a due date that goes
		backwards silently misstates the whole ledger column.
		"""
		previous = None

		for row in self.repayment_schedule:
			if not row.due_date:
				continue

			due = getdate(row.due_date)
			if previous and due <= previous:
				frappe.throw(
					_(
						"Instalment {0} falls due on {1}, which is not after the previous instalment's {2}. Due dates must run forwards."
					).format(row.idx, frappe.bold(formatdate(due)), frappe.bold(formatdate(previous))),
					title=_("Due Dates Out of Order"),
				)
			previous = due

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

	def validate_live_schedule(self):
		"""Refuse anything that only makes sense on the facility's live schedule.

		Payments belong to the one schedule that currently represents what is
		owed. Recording them against a draft, a superseded revision or a
		cancelled document would put the money somewhere nothing reads, and the
		facility's figures would never see it.
		"""
		if self.docstatus == 1 and self.is_active and self.status == "Active":
			return

		frappe.throw(
			_("{0} is not the live schedule for {1}, so payments cannot be recorded against it.").format(
				frappe.bold(self.name), frappe.bold(self.loan)
			),
			title=_("Not the Live Schedule"),
		)

	def apply_derived_balances(self):
		"""Recompute the ledger columns on schedules that were not generated here.

		Imported rows and manual ones carry the bank's principal and interest
		but no balances, or balances typed by hand that quietly stop agreeing
		after an edit. Deriving them on every save keeps the outstanding,
		instalment and closing columns following from the amounts beside them.

		Generated schedules are left alone: the engine already produced their
		balances, and recomputing would be a no-op at best.
		"""
		if self.docstatus != 0 or not self.repayment_schedule:
			return

		if self.schedule_source not in DERIVED_BALANCE_SOURCES and self.repayment_method != MANUAL:
			return

		derived = derive_balances(
			[row.as_dict() for row in self.repayment_schedule],
			self.disbursed_amount,
			capitalised_periods=self.capitalised_periods(),
		)

		for row, values in zip(self.repayment_schedule, derived, strict=True):
			row.outstanding_principal = values["outstanding_principal"]
			row.instalment_amount = values["instalment_amount"]
			row.closing_principal = values["closing_principal"]

	def capitalised_periods(self):
		"""Leading instalments whose interest is rolled up rather than billed."""
		if self.moratorium_type != INTEREST_CAPITALISED:
			return 0

		return get_moratorium_periods(self.moratorium_months, self.repayment_frequency or "Monthly")

	def validate_settled_instalments_carried_over(self):
		"""A revision must not lose a repayment the schedule it replaces recorded.

		Regenerating a schedule is how a rate revision or a restructure is
		handled, and the new rows are matched to the old ones by due date. If
		the schedule being superseded has instalments marked paid, the
		replacement has to agree they were paid, or the facility would show
		money owing that the bank has already received.
		"""
		live = self.get_live_schedule()
		if not live:
			return

		settled = {
			getdate(row.due_date)
			for row in live.repayment_schedule
			if row.payment_status in ("Paid", "Partially Paid") and row.due_date
		}
		if not settled:
			return

		paid_here = {
			getdate(row.due_date)
			for row in self.repayment_schedule
			if row.payment_status == "Paid" and row.due_date
		}
		missing = sorted(settled - paid_here)

		if missing:
			frappe.throw(
				_(
					"{0} already records these instalments as settled: {1}. Mark them Paid here before submitting, or the repayments would be lost."
				).format(
					frappe.bold(live.name),
					frappe.bold(", ".join(formatdate(due) for due in missing)),
				),
				title=_("Settled Instalments Not Carried Over"),
			)

	def get_live_schedule(self):
		"""The facility's currently active schedule, if it is not this one."""
		name = frappe.db.get_value(
			"Debt Repayment Schedule",
			{"loan": self.loan, "is_active": 1, "docstatus": 1, "name": ("!=", self.name)},
			"name",
		)
		return frappe.get_doc("Debt Repayment Schedule", name) if name else None

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
		# run_doc_method checks read permission only, so a mutating method has
		# to assert write access itself or any reader could call it.
		self.check_permission("write")

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
		# run_doc_method checks read permission only, so a mutating method has
		# to assert write access itself or any reader could call it.
		self.check_permission("write")

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
					"total_principal_paid",
					"total_interest_paid",
					"next_due_amount",
				),
				0,
			)
			values["next_due_date"] = None
			# Losing the schedule does not repay the loan. Without a schedule
			# the facility still owes what it drew, so the balance goes back to
			# the disbursed amount rather than to zero, which would read as a
			# settled debt.
			values["outstanding_principal"] = flt(facility.disbursed_amount)
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

		This is the "entry already posted" path: the accountant has booked the
		repayment themselves and is telling the schedule about it. Nothing is
		posted here, and entry_posted_by_system stays 0 so a later cancellation
		leaves their journal alone.

		A receipt short of the instalment leaves the row Partially Paid; the
		shortfall keeps the row in the running for next-due. Overpayment is
		refused rather than absorbed: money beyond the instalment is a
		prepayment, which changes the rest of the schedule and belongs in
		Recalculate from Instalment, not in a single row.
		"""
		self.check_permission("write")
		self.reload()
		self.validate_live_schedule()

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

		validate_settlement_date(paid_date, row.due_date, row.instalment_number)

		if error := journal_entry_reference_error(payment_entry, self.company):
			frappe.throw(error, title=_("Unusable Journal Entry Reference"))

		settled = abs(paid_amount - due) <= RECONCILIATION_TOLERANCE
		values = {
			"payment_status": "Paid" if settled else "Partially Paid",
			"paid_date": getdate(paid_date),
			"paid_amount": paid_amount,
			"payment_entry": payment_entry,
			"remarks": remarks,
			# This app did not post the entry, so cancelling the schedule must
			# never reverse it.
			"entry_posted_by_system": 0,
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
		self.check_permission("write")

		if self.docstatus == 1:
			self.validate_live_schedule()

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

	# --- posting ------------------------------------------------------------

	@frappe.whitelist()
	def make_payment_entry(
		self,
		instalment_number,
		posting_date,
		bank_account=None,
		reference_no=None,
		loan_account=None,
		interest_account=None,
	):
		"""Post one instalment's repayment to the ledger and mark the row Paid.

		This is the other half of mark_instalment_paid: there the accountant
		has already booked the entry, here the app books it. Either way the row
		ends up Paid, but only this path sets entry_posted_by_system, because
		only this path may reverse the entry later.

		The row is locked for the length of the transaction before anything is
		read off it. Two people pressing the button on the same instalment
		would otherwise both find it unpaid and post the repayment twice.
		"""
		self.check_permission("write")
		self.reload()
		self.validate_live_schedule()

		index = self.get_instalment_index(instalment_number)
		row = self.repayment_schedule[index]

		# Locks this child row until the transaction ends.
		status = frappe.db.get_value(
			"Debt Repayment Schedule Detail", row.name, "payment_status", for_update=True
		)
		if status not in PAYABLE_STATUSES:
			frappe.throw(
				_("Instalment {0} is {1}, so no payment can be posted against it.").format(
					row.instalment_number, _(status)
				),
				title=_("Not Payable"),
			)

		validate_settlement_date(posting_date, row.due_date, row.instalment_number)

		facility = frappe.get_doc("Debt Facility", self.loan)
		reference_no = reference_no or f"{self.name}/{row.instalment_number}"
		capitalised = self.is_capitalised_row(index, row)
		accounts = self.resolve_posting_accounts(facility, loan_account, interest_account)

		if capitalised:
			voucher_type = "Journal Entry"
			lines = self.capitalisation_lines(row, accounts)
		else:
			voucher_type = "Bank Entry"
			lines = self.repayment_lines(facility, row, bank_account, accounts)

		entry = make_journal_entry(
			company=self.company,
			posting_date=posting_date,
			voucher_type=voucher_type,
			reference_no=reference_no,
			remark=_("Instalment {0} of {1}").format(row.instalment_number, self.loan),
			loan_provider=self.loan_provider,
			lines=lines,
		)

		for fieldname, value in (
			("payment_status", "Paid"),
			("paid_date", getdate(posting_date)),
			("paid_amount", flt(row.instalment_amount)),
			("payment_entry", entry),
			("entry_posted_by_system", 1),
		):
			row.db_set(fieldname, value)

		self.reload()
		self.refresh_totals()
		self.update_facility()
		return entry

	@frappe.whitelist()
	def reverse_payment(self, instalment_number):
		"""Undo one settled instalment, cancelling the entry if this app posted it.

		The row is cleared before the Journal Entry is cancelled, and the order
		matters. Frappe's cancel-time link check looks for submitted documents
		pointing at the entry, and it reads the *child row's* own docstatus,
		which on a submitted schedule is 1. While the row still holds the link
		the entry cannot be cancelled; once cleared, it can.

		An entry somebody recorded by hand is left alone -- this app did not
		post it and has no business reversing it -- and only the row is reset.
		"""
		self.check_permission("write")
		self.reload()
		self.validate_live_schedule()

		row = self.get_instalment(instalment_number)

		if row.payment_status not in ("Paid", "Partially Paid"):
			frappe.throw(
				_("Instalment {0} is {1}. There is nothing to reverse.").format(
					row.instalment_number, _(row.payment_status)
				),
				title=_("Nothing to Reverse"),
			)

		entry = row.payment_entry if row.entry_posted_by_system else None

		for fieldname, value in (
			("payment_status", "Unpaid"),
			("paid_date", None),
			("paid_amount", 0),
			("payment_entry", None),
			("entry_posted_by_system", 0),
		):
			row.db_set(fieldname, value)

		cancelled = cancel_journal_entries([entry])

		self.reload()
		self.refresh_totals()
		self.update_facility()
		return cancelled[0] if cancelled else None

	def is_capitalised_row(self, index, row):
		"""Whether this instalment's interest is rolled into the balance, not billed."""
		return index < self.capitalised_periods() and not flt(row.principal_amount)

	def resolve_posting_accounts(self, facility, loan_account, interest_account):
		"""Which loan and interest ledgers this entry posts against.

		Each falls back to the facility's own account. An override is checked
		the same way the facility's field would be, so the dialog cannot post
		somewhere the facility itself would have refused.
		"""
		resolved = {
			"loan_account": loan_account or facility.loan_liability_account,
			"interest_account": interest_account or facility.interest_expense_account,
		}

		if loan_account:
			validate_ledger_account(
				loan_account, company=self.company, root_type="Liability", label=_("Loan Account")
			)

		if interest_account:
			validate_ledger_account(
				interest_account,
				company=self.company,
				root_type="Expense",
				label=_("Interest Account"),
			)

		return resolved

	def capitalisation_lines(self, row, accounts):
		"""Non-cash lines for an instalment inside a capitalising moratorium.

		No money moves: the interest accrues and is added to what is owed. So
		the expense is recognised against the loan account rather than a bank
		account, which is what grows the balance the later instalments repay.
		"""
		interest = money(row.interest_amount) + money(row.other_charges)

		if interest == ZERO:
			frappe.throw(
				_("Instalment {0} accrues no interest, so there is nothing to capitalise.").format(
					row.instalment_number
				),
				title=_("Nothing to Post"),
			)

		self.require_posting_account(accounts, "interest_account", _("Interest Account"))
		self.require_posting_account(accounts, "loan_account", _("Loan Account"))

		return [
			(accounts["interest_account"], interest, ZERO),
			(accounts["loan_account"], ZERO, interest),
		]

	def repayment_lines(self, facility, row, bank_account, accounts):
		"""Lines for an ordinary repayment: principal and interest out of the bank."""
		principal = money(row.principal_amount)
		interest = money(row.interest_amount) + money(row.other_charges)
		total = principal + interest

		if total != money(row.instalment_amount):
			frappe.throw(
				_(
					"Instalment {0} adds up to {1}, but the row says {2}. Fix the schedule before posting."
				).format(
					row.instalment_number,
					frappe.bold(self.fmt(total)),
					frappe.bold(self.fmt(row.instalment_amount)),
				),
				title=_("Instalment Does Not Add Up"),
			)

		if principal != ZERO:
			self.require_posting_account(accounts, "loan_account", _("Loan Account"))
		if interest != ZERO:
			self.require_posting_account(accounts, "interest_account", _("Interest Account"))

		bank = self.resolve_bank_account(facility, bank_account)
		validate_bank_account(
			bank,
			company=self.company,
			loan_account=accounts["loan_account"],
			label=_("Bank Account"),
		)

		return [
			(accounts["loan_account"], principal, ZERO),
			(accounts["interest_account"], interest, ZERO),
			(bank, ZERO, total),
		]

	def require_posting_account(self, accounts, key, label):
		"""Insist on an account this instalment cannot be posted without."""
		if accounts.get(key):
			return

		frappe.throw(
			_("No {0} to post against. Set one on {1}, or choose one on the payment.").format(
				label, frappe.bold(self.loan)
			),
			title=_("Account Missing"),
		)

	def resolve_bank_account(self, facility, bank_account):
		"""The ledger the repayment leaves from, checked before anything is posted.

		Falls back to the facility's Bank Account (Repayment of Loan) and
		to nothing else. It deliberately does NOT fall back to the receipt bank:
		money often arrives in one account and is repaid by mandate from
		another, and quietly crediting the wrong one is the kind of error that
		reconciles to nobody.
		"""
		account = bank_account or facility.repayment_bank_account

		if not account:
			frappe.throw(
				_(
					"No bank account to pay from. Set the Bank Account (Repayment of Loan) on {0}, or choose one on the payment."
				).format(frappe.bold(facility.name)),
				title=_("Bank Account Required"),
			)

		return account

	# --- upload -------------------------------------------------------------

	@frappe.whitelist()
	def import_schedule(self, file_url):
		"""Replace the draft's rows with the ones in an uploaded spreadsheet.

		Every problem the file has is reported in one message. A user
		correcting a bank annexure should see the whole list, not discover the
		next bad cell after each re-upload.

		Nothing is written until the file parses clean, reconciles against the
		facility and never drives the balance negative, so a rejected upload
		leaves the existing rows exactly as they were.
		"""
		self.check_permission("write")

		if self.docstatus != 0:
			frappe.throw(_("Only a draft schedule can be imported into."), title=_("Not a Draft"))

		rows, errors = parse_schedule_rows(
			read_spreadsheet(file_url),
			dayfirst=date_format_is_dayfirst(),
			today=getdate(nowdate()),
			disbursement_date=self.facility_disbursement_date(),
		)

		# Parser messages quote raw spreadsheet cells, so they are escaped here.
		# The two below are built with frappe.bold and escape their own user
		# text already; escaping them again would print the markup.
		errors = [escape_html(error) for error in errors]
		errors.extend(self.reference_errors(rows))
		errors.extend(self.balance_errors(rows))

		if errors:
			self.throw_import_errors(errors)

		self.set("repayment_schedule", [])
		for row in rows:
			self.append("repayment_schedule", self.import_row(row))

		self.schedule_source = "Imported from Sanction Letter"
		self.renumber_instalments()
		self.apply_derived_balances()
		self.calculate_totals()
		self.warn_if_schedule_disagrees_with_facility(rows)
		return len(rows)

	def import_row(self, row):
		"""One imported row, with the payment fields filled in for a settled one.

		A Paid row is dated by the entry it names, because that is when the
		money actually moved; without an entry the due date is the best
		available answer. The amount settled is the instalment itself -- the
		file records that the instalment was paid, not a part payment.
		"""
		values = {
			"due_date": row["due_date"],
			"principal_amount": row["principal_amount"],
			"interest_amount": row["interest_amount"],
			"payment_status": row["payment_status"],
			"payment_entry": row["payment_entry"],
			"entry_posted_by_system": 0,
		}

		if row["payment_status"] == "Paid":
			posted_on = (
				frappe.db.get_value("Journal Entry", row["payment_entry"], "posting_date")
				if row["payment_entry"]
				else None
			)
			values["paid_date"] = posted_on or row["due_date"]
			values["paid_amount"] = flt(row["principal_amount"]) + flt(row["interest_amount"])

		return values

	def reference_errors(self, rows):
		"""Check every Journal Entry the file names is one we can actually use."""
		errors = []

		for index, row in enumerate(rows, start=1):
			if not row["payment_entry"]:
				continue

			if error := journal_entry_reference_error(row["payment_entry"], self.company):
				errors.append(_("Instalment {0}: {1}").format(index, error))

		return errors

	def balance_errors(self, rows):
		"""Report the first instalment that would repay more than is outstanding.

		Only the first is reported: once the balance has gone negative every
		row after it is wrong too, and listing forty of them hides the one that
		actually needs fixing.
		"""
		derived = derive_balances(rows, self.disbursed_amount, capitalised_periods=self.capitalised_periods())

		for index, row in enumerate(derived, start=1):
			if money(row["closing_principal"]) < ZERO:
				return [
					_(
						"Instalment {0} repays {1} against an outstanding {2}, which would take the balance below zero."
					).format(
						index,
						frappe.bold(self.fmt(row["principal_amount"])),
						frappe.bold(self.fmt(row["outstanding_principal"])),
					)
				]

		return []

	def throw_import_errors(self, errors):
		"""Show every problem at once, capped so the dialog stays readable."""
		shown = errors[:MAX_REPORTED_ERRORS]
		listed = "<br>".join(shown)

		if len(errors) > MAX_REPORTED_ERRORS:
			listed += "<br>" + _("...and {0} more.").format(len(errors) - MAX_REPORTED_ERRORS)

		frappe.throw(listed, title=_("The File Could Not Be Imported"))

	def warn_if_schedule_disagrees_with_facility(self, rows):
		"""Flag, without blocking, a schedule that does not match the facility.

		A sanction letter can legitimately disagree -- a part disbursement, or
		a bank that rounds its own annexure -- so this warns rather than
		refuses. Submission still enforces the reconciliation.
		"""
		principal = sum((money(row["principal_amount"]) for row in rows), ZERO)
		disbursed = money(self.disbursed_amount)

		if principal != disbursed:
			frappe.msgprint(
				_("The imported rows repay {0}, but {1} was disbursed.").format(
					frappe.bold(self.fmt(principal)), frappe.bold(self.fmt(disbursed))
				),
				title=_("Principal Does Not Match"),
				indicator="orange",
			)

		expected = int(self.number_of_instalments or 0)
		if expected and len(rows) != expected:
			frappe.msgprint(
				_("The file has {0} instalments, but the facility says {1}.").format(
					frappe.bold(len(rows)), frappe.bold(expected)
				),
				title=_("Instalment Count Does Not Match"),
				indicator="orange",
			)

	def facility_disbursement_date(self):
		date = frappe.db.get_value("Debt Facility", self.loan, "disbursement_date") if self.loan else None
		return getdate(date) if date else None

	def fmt(self, value):
		"""Format an amount in the facility's company currency for a message."""
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		return frappe.utils.fmt_money(flt(value), currency=currency)


# --- spreadsheet helpers ----------------------------------------------------


def date_format_is_dayfirst() -> bool:
	"""Whether this site writes the day before the month.

	A bare "05-08-2025" is two different dates depending on the answer, and the
	only defensible source is the site's own setting: a file exported from here
	then reads back as what it was written as. Indian sites default to
	day-first, which is also the fallback when the setting is unreadable.
	"""
	fmt = (frappe.get_system_settings("date_format") or "dd-mm-yyyy").lower()

	if "d" in fmt and "m" in fmt:
		return fmt.index("d") < fmt.index("m")

	return True


def read_spreadsheet(file_url):
	"""Rows from an uploaded .xlsx or .csv, as lists of cells.

	The File is loaded as a document and permission-checked before a byte is
	read: a file_url is guessable, and this method would otherwise read any
	attachment on the site. Content is taken as raw bytes, because letting
	frappe decode it would corrupt the zip an xlsx really is.
	"""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	file_doc.check_permission("read")

	content = file_doc.get_content(encodings=[])
	name = (file_doc.file_name or file_url).lower()

	if name.endswith(".csv"):
		return read_csv_content(content)

	if name.endswith((".xlsx", ".xlsm")):
		return read_xlsx_file_from_attached_file(fcontent=content)

	frappe.throw(
		_("{0} is not a spreadsheet. Upload an .xlsx or .csv file.").format(
			frappe.bold(frappe.utils.escape_html(file_doc.file_name or file_url))
		),
		title=_("Unsupported File"),
	)


@frappe.whitelist()
def download_schedule_template(schedule=None):
	"""Send the upload template, optionally filled with a schedule's own rows.

	Round-tripping is the point: a user downloads the template for a schedule,
	edits it and uploads it back, so the due date column carries a real date
	format rather than a serial number or a date-time, and the parser reads
	back what this wrote.
	"""
	rows = []

	if schedule:
		doc = frappe.get_doc("Debt Repayment Schedule", schedule)
		doc.check_permission("read")
		rows = [
			[
				getdate(row.due_date) if row.due_date else None,
				flt(row.principal_amount),
				flt(row.interest_amount),
				row.payment_status,
				row.payment_entry,
			]
			for row in doc.repayment_schedule
		]

	data = [[_(label) for _fieldname, label in TEMPLATE_COLUMNS], *rows]

	xlsx = make_xlsx(
		data,
		"Repayment Schedule",
		column_widths=[14, 18, 16, 12, 24],
		styles={
			# A date column, not a date-time one: the instalment falls due on a
			# day, and a trailing 00:00:00 only invites a parsing argument.
			"styles": [{"num_format": "dd-mm-yyyy"}, {"bold": True}],
			"column_styles": {0: [0]},
			"row_styles": {0: [1]},
		},
	)

	provide_binary_file(schedule or "Repayment Schedule Template", "xlsx", xlsx.getvalue())
