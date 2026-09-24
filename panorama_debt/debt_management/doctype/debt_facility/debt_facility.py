# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, formatdate, getdate

from panorama_debt.debt_management.doctype.loan_provider.loan_provider import get_provider_accounts

# Facility types that revolve: drawn and repaid at will against a limit, with no
# instalment schedule to amortise.
REVOLVING_LOAN_TYPES = ("Cash Credit", "Overdraft")

# Term-loan fields that make no sense on a revolving facility and are cleared
# rather than left to go stale behind a hidden section.
REPAYMENT_TERM_FIELDS = (
	"tenure_months",
	"moratorium_months",
	"moratorium_type",
	"repayment_method",
	"repayment_start_date",
	"number_of_instalments",
)

# Which side of the books each posting account has to sit on.
ACCOUNT_ROOT_TYPES = {
	"loan_liability_account": "Liability",
	"interest_expense_account": "Expense",
}


class DebtFacility(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amended_from: DF.Link | None
		bank_gl_account: DF.Link | None
		benchmark: DF.Literal["", "I-EBLR", "MCLR", "Repo Linked", "Other"]
		benchmark_rate: DF.Percent
		company: DF.Link
		disbursed_amount: DF.Currency
		disbursement_date: DF.Date | None
		drawing_power: DF.Currency
		in_scope: DF.Check
		interest_expense_account: DF.Link | None
		interest_rate_type: DF.Literal["Fixed", "Floating"]
		is_revolving: DF.Check
		limit_review_date: DF.Date | None
		loan_account_number: DF.Data | None
		loan_liability_account: DF.Link | None
		loan_provider: DF.Link
		loan_type: DF.Literal[
			"Term Loan",
			"Working Capital Term Loan",
			"Cash Credit",
			"Overdraft",
			"COVID Loan",
			"Channel Finance",
			"Peak Season",
			"Inter-Corporate Deposit",
			"Other",
		]
		moratorium_months: DF.Int
		moratorium_type: DF.Literal["", "Interest Servicing", "Full Moratorium (Interest Capitalised)"]
		naming_series: DF.Literal["LOAN-.YYYY.-"]
		next_due_amount: DF.Currency
		next_due_date: DF.Date | None
		number_of_instalments: DF.Int
		outstanding_principal: DF.Currency
		provider_type: DF.Data | None
		purpose: DF.Data | None
		rate_of_interest: DF.Percent
		repayment_frequency: DF.Literal["Monthly", "Quarterly", "Half Yearly", "Yearly"]
		repayment_method: DF.Literal[
			"",
			"Equal Instalment (EMI)",
			"Equal Principal",
			"Interest Only then EMI",
			"Interest Only then Equal Principal",
			"Bullet",
			"As per Bank Schedule (Manual)",
		]
		repayment_start_date: DF.Date | None
		sanction_date: DF.Date
		sanction_letter: DF.Attach | None
		sanctioned_amount: DF.Currency
		sanctioned_limit: DF.Currency
		spread: DF.Percent
		status: DF.Literal["Draft", "Active", "Closed", "Foreclosed", "Cancelled"]
		tenure_months: DF.Int
		total_interest_paid: DF.Currency
		total_interest_payable: DF.Currency
		total_payable: DF.Currency
		total_principal_paid: DF.Currency
	# end: auto-generated types

	def validate(self):
		self.set_is_revolving()
		self.set_rate_of_interest()
		self.apply_provider_account_defaults()
		self.validate_amounts()
		self.validate_dates()
		self.validate_repayment_terms()
		self.validate_accounts()
		self.set_status()

	def before_submit(self):
		"""Open the facility: it goes Active and starts owing what was drawn.

		Outstanding principal is seeded from the disbursed amount rather than
		the sanctioned amount, because a sanction the group has not drawn is
		not yet a debt. A revolving facility is left at zero -- its utilisation
		is read live from the GL, never carried on this document.
		"""
		self.status = "Active"

		if not self.is_revolving and not flt(self.outstanding_principal):
			self.outstanding_principal = flt(self.disbursed_amount)

	def on_cancel(self):
		"""Mark a cancelled facility so it stops reading as live debt.

		Written with db_set because the status fields are read-only and the
		document is already past submission at this point.
		"""
		self.db_set("status", "Cancelled")

	def set_is_revolving(self):
		"""Derive the revolving flag from the facility type, never by hand.

		Cash Credit and Overdraft revolve; everything else amortises. The term
		fields are cleared when the flag is on so a facility switched to CC
		cannot keep a stale tenure behind the hidden section and have a
		schedule generated from it later.
		"""
		self.is_revolving = 1 if self.loan_type in REVOLVING_LOAN_TYPES else 0

		if self.is_revolving:
			for fieldname in REPAYMENT_TERM_FIELDS:
				self.set(fieldname, None)

	def set_rate_of_interest(self):
		"""Derive a floating facility's effective rate as benchmark plus spread.

		Only filled when the rate is still blank. It is deliberately not
		recomputed on every save: a negotiated rate often differs from the
		arithmetic, and quietly overwriting it would lose the negotiation. The
		client script recomputes on demand when the user edits either input.
		"""
		if self.interest_rate_type != "Floating" or flt(self.rate_of_interest):
			return

		if flt(self.benchmark_rate) or flt(self.spread):
			self.rate_of_interest = flt(self.benchmark_rate) + flt(self.spread)

	def apply_provider_account_defaults(self):
		"""Fill blank posting accounts from the provider's row for this company.

		Only blanks are filled, so a deliberate override survives a re-save. If
		the company is later changed, the carried-over accounts will fail
		validate_accounts below rather than being silently repointed -- the
		user is told which account belongs to the wrong company.
		"""
		if not (self.loan_provider and self.company):
			return

		for fieldname, account in get_provider_accounts(self.loan_provider, self.company).items():
			if account and not self.get(fieldname):
				self.set(fieldname, account)

	def validate_amounts(self):
		"""Check the money adds up before anything downstream relies on it."""
		if flt(self.sanctioned_amount) <= 0:
			frappe.throw(_("Sanctioned Amount must be greater than zero."), title=_("Invalid Amount"))

		if flt(self.disbursed_amount) > flt(self.sanctioned_amount):
			frappe.throw(
				_("Disbursed Amount ({0}) cannot exceed Sanctioned Amount ({1}).").format(
					frappe.bold(self.fmt_money(self.disbursed_amount)),
					frappe.bold(self.fmt_money(self.sanctioned_amount)),
				),
				title=_("Disbursement Exceeds Sanction"),
			)

		# A zero rate is valid -- the group holds genuinely interest-free
		# inter-corporate loans -- but a negative one never is.
		if flt(self.rate_of_interest) < 0:
			frappe.throw(_("Rate of Interest cannot be negative."), title=_("Invalid Rate"))

		if self.is_revolving and flt(self.drawing_power) > flt(self.sanctioned_limit):
			frappe.throw(
				_("Drawing Power ({0}) cannot exceed the Sanctioned Limit ({1}).").format(
					frappe.bold(self.fmt_money(self.drawing_power)),
					frappe.bold(self.fmt_money(self.sanctioned_limit)),
				),
				title=_("Drawing Power Exceeds Limit"),
			)

	def validate_dates(self):
		"""Nothing about a facility may predate its sanction."""
		for fieldname in ("disbursement_date", "repayment_start_date"):
			value = self.get(fieldname)
			if value and getdate(value) < getdate(self.sanction_date):
				frappe.throw(
					_("{0} ({1}) cannot be before the Sanction Date ({2}).").format(
						_(self.meta.get_label(fieldname)),
						frappe.bold(formatdate(value)),
						frappe.bold(formatdate(self.sanction_date)),
					),
					title=_("Date Before Sanction"),
				)

	def validate_repayment_terms(self):
		"""Check a term facility can actually be amortised.

		Skipped entirely for revolving facilities, which have no schedule. The
		moratorium check mirrors the guard in the schedule engine, but fails
		here with a message naming the two figures, rather than later when the
		user presses Generate Schedule.
		"""
		if self.is_revolving:
			return

		if int(self.number_of_instalments or 0) <= 0:
			frappe.throw(
				_("Number of Instalments must be greater than zero for a {0}.").format(
					frappe.bold(_(self.loan_type))
				),
				title=_("Repayment Terms Incomplete"),
			)

		if int(self.moratorium_months or 0) < 0:
			frappe.throw(_("Moratorium (Months) cannot be negative."), title=_("Invalid Moratorium"))

		if int(self.moratorium_months or 0) >= int(self.number_of_instalments or 0):
			frappe.throw(
				_(
					"A moratorium of {0} months covers all {1} instalments, leaving nothing to repay the principal."
				).format(frappe.bold(self.moratorium_months), frappe.bold(self.number_of_instalments)),
				title=_("Moratorium Too Long"),
			)

	def validate_accounts(self):
		"""Vet every posting account: postable, right side, right company."""
		for fieldname, expected_root_type in ACCOUNT_ROOT_TYPES.items():
			self.validate_account(fieldname, expected_root_type)

		if self.is_revolving:
			self.validate_account("bank_gl_account", None)

	def validate_account(self, fieldname, expected_root_type):
		"""Check one account. A blank is allowed; the GL posting step will ask for it.

		``expected_root_type`` of None means only the group and company checks
		apply, which is the case for the CC/OD bank ledger: groups book those
		either as a liability or as a negative bank asset, and both are valid.
		"""
		account = self.get(fieldname)
		if not account:
			return

		root_type, is_group, account_company = frappe.db.get_value(
			"Account", account, ["root_type", "is_group", "company"]
		)
		label = _(self.meta.get_label(fieldname))

		if is_group:
			frappe.throw(
				_("{0} {1} is a group account. Pick a ledger account that can be posted to.").format(
					label, frappe.bold(account)
				),
				title=_("Group Account Selected"),
			)

		if expected_root_type and root_type != expected_root_type:
			frappe.throw(
				_("{0} {1} is {2}, but it has to be {3}.").format(
					label, frappe.bold(account), frappe.bold(_(root_type)), frappe.bold(_(expected_root_type))
				),
				title=_("Wrong Account Type"),
			)

		if account_company != self.company:
			frappe.throw(
				_("{0} {1} belongs to {2}, not to {3}.").format(
					label, frappe.bold(account), frappe.bold(account_company), frappe.bold(self.company)
				),
				title=_("Account Belongs to Another Company"),
			)

	def set_status(self):
		"""Keep the status in step with the document state.

		Only the draft and freshly-submitted transitions are handled here.
		Closed and Foreclosed are end states reached deliberately elsewhere, so
		a submitted facility already carrying one is left alone.
		"""
		if self.docstatus == 0:
			self.status = "Draft"
		elif self.docstatus == 1 and self.status in (None, "", "Draft"):
			self.status = "Active"

	def fmt_money(self, value):
		"""Format an amount in the facility's own company currency for a message."""
		return frappe.utils.fmt_money(flt(value), currency=get_company_currency(self.company))


def get_company_currency(company):
	"""Default currency of a company, or None when the company is not set yet."""
	return frappe.get_cached_value("Company", company, "default_currency") if company else None
