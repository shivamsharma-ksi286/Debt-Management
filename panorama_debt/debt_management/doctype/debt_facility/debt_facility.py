# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, formatdate, get_link_to_form, getdate

from panorama_debt.debt_management.accounting import (
	cancel_journal_entries,
	make_journal_entry,
	validate_not_future,
)
from panorama_debt.debt_management.doctype.loan_provider.loan_provider import get_provider_accounts
from panorama_debt.debt_management.schedule_engine import ZERO, money

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
	"bank_account",
	"repayment_bank_account",
	"disbursement_entry_posted",
)

# Ledgers the disbursed money may land in. A borrowing is received into a bank
# or cash account; anything else means the wrong field was picked.
BANK_ACCOUNT_TYPES = ("Bank", "Cash")

# Figures a freshly submitted facility must start from, whatever a copied or
# amended document arrived carrying.
ZEROED_ON_SUBMIT = (
	"total_principal_paid",
	"total_interest_paid",
	"next_due_amount",
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
		bank_account: DF.Link | None
		bank_gl_account: DF.Link | None
		benchmark: DF.Literal["", "I-EBLR", "MCLR", "Repo Linked", "Other"]
		benchmark_rate: DF.Percent
		company: DF.Link
		disbursed_amount: DF.Currency
		disbursement_date: DF.Date | None
		disbursement_entry_posted: DF.Literal["", "Yes", "No"]
		disbursement_journal_entry: DF.Link | None
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
		repayment_bank_account: DF.Link | None
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
		self.validate_disbursement_entry_posted()
		self.validate_accounts()
		self.validate_bank_accounts()
		self.set_status()

	def before_submit(self):
		"""Open the facility: it goes Active and starts owing what was drawn.

		Outstanding principal is seeded from the disbursed amount rather than
		the sanctioned amount, because a sanction the group has not drawn is
		not yet a debt. It is set unconditionally, along with zeroing the paid
		and next-due figures, so an amended or copied facility cannot open
		carrying the previous document's position. A revolving facility is left
		alone -- its utilisation is read live from the GL, never carried here.
		"""
		self.status = "Active"

		if self.is_revolving:
			return

		self.validate_posting_accounts()
		self.validate_disbursement_ready()

		self.outstanding_principal = flt(self.disbursed_amount)
		for fieldname in ZEROED_ON_SUBMIT:
			self.set(fieldname, 0)
		self.next_due_date = None

	def on_submit(self):
		"""Book the receipt of the money, when the group has not booked it already.

		Only "No" posts: it is the user saying the ledger does not yet know
		about this disbursement. The field is then flipped to "Yes" so the
		answer on a submitted facility reads as the state of the world rather
		than as the instruction that produced it, and so an amendment cannot
		post the same receipt twice.
		"""
		if self.is_revolving or self.disbursement_entry_posted != "No":
			return

		entry = make_journal_entry(
			company=self.company,
			posting_date=self.disbursement_date,
			voucher_type="Bank Entry",
			reference_no=self.name,
			remark=_("Disbursement of {0}").format(self.name),
			loan_provider=self.loan_provider,
			lines=[
				(self.bank_account, money(self.disbursed_amount), ZERO),
				(self.loan_liability_account, ZERO, money(self.disbursed_amount)),
			],
		)

		self.db_set("disbursement_journal_entry", entry)
		self.db_set("disbursement_entry_posted", "Yes")

		frappe.msgprint(
			_("Disbursement posted as {0}.").format(get_link_to_form("Journal Entry", entry)),
			alert=True,
			indicator="green",
		)

	def on_cancel(self):
		"""Reverse the disbursement entry and mark the facility cancelled.

		This has to run in on_cancel rather than before_cancel. By the time
		on_cancel fires, _cancel has already saved docstatus 2, so this
		facility no longer counts as a submitted back-link and the Journal
		Entry will cancel. From before_cancel the facility would still be
		submitted and its own link would block the entry.

		Only the disbursement entry is touched. Repayment entries belong to the
		schedule and are cancelled by it.
		"""
		cancel_journal_entries([self.disbursement_journal_entry])
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

	def validate_disbursement_entry_posted(self):
		"""A term facility has to say whether the receipt is already in the books.

		There is deliberately no default. The answer decides whether the system
		posts a Journal Entry on submit, and a wrong default would either
		double-book a disbursement the accountant already entered or leave the
		ledger silently short by the whole loan.
		"""
		if self.is_revolving or self.disbursement_entry_posted:
			return

		frappe.throw(
			_(
				"Say whether the disbursement entry has already been posted. There is no default, because the answer decides whether this facility posts its own Journal Entry."
			),
			title=_("Disbursement Entry Posted Is Required"),
		)

	def validate_bank_accounts(self):
		"""Vet both bank fields through the one shared rule."""
		for fieldname in ("bank_account", "repayment_bank_account"):
			if self.get(fieldname):
				validate_bank_account(
					self.get(fieldname),
					company=self.company,
					loan_account=self.loan_liability_account,
					label=_(self.meta.get_label(fieldname)),
				)

	def before_update_after_submit(self):
		"""Re-vet what an Update on a submitted facility is allowed to change.

		Frappe does not run validate() on this path -- only before_save and
		before_submit branches do -- so without this the one field that stays
		editable after submission would never be checked again.
		"""
		self.validate_bank_accounts()

	def validate_posting_accounts(self):
		"""Require the accounts the facility will post to, at submission time.

		Left out of validate() so a facility can be drafted before the chart of
		accounts is ready. By submission the accounts have to exist: the
		disbursement posts against the loan account, and every repayment will
		post interest against the interest account. An interest-free
		inter-corporate loan never posts interest, so it never needs one.
		"""
		if not self.loan_liability_account:
			frappe.throw(
				_(
					"Set the Loan Account before submitting. The disbursement and every repayment post against it."
				),
				title=_("Loan Account Required"),
			)

		if not self.repayment_bank_account:
			frappe.throw(
				_(
					"Set the Bank Account (Repayment of Loan) before submitting. Every instalment entry credits it."
				),
				title=_("Repayment Bank Account Required"),
			)

		if flt(self.rate_of_interest) > 0 and not self.interest_expense_account:
			frappe.throw(
				_(
					"Set the Interest Account before submitting. This facility carries interest at {0}%."
				).format(frappe.bold(self.rate_of_interest)),
				title=_("Interest Account Required"),
			)

	def validate_disbursement_ready(self):
		"""Check there is a disbursement to post before promising to post one."""
		if self.disbursement_entry_posted != "No":
			return

		if flt(self.disbursed_amount) <= 0:
			frappe.throw(
				_(
					"Nothing has been disbursed, so there is no entry to post. Enter the Disbursed Amount, or set Disbursement Entry Posted to Yes."
				),
				title=_("Nothing to Disburse"),
			)

		if not self.disbursement_date:
			frappe.throw(
				_("Set the Disbursement Date. It is the posting date of the entry this facility will book."),
				title=_("Disbursement Date Required"),
			)

		validate_not_future(self.disbursement_date, "Disbursement Date")

		if not self.bank_account:
			frappe.throw(
				_(
					"Set the Bank Account the money was received into, so the disbursement entry has somewhere to debit."
				),
				title=_("Bank Account Required"),
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


def validate_ledger_account(account, *, company, root_type, label):
	"""Check a posting account a caller chose is one this company can post to.

	The payment dialog lets a user override which loan or interest ledger an
	instalment posts against -- a facility can be reclassified, or one month's
	interest booked to a different head. The override still has to be a real
	postable ledger of the borrowing company on the right side of the books,
	which is exactly what the facility's own fields are held to.
	"""
	detail = frappe.db.get_value("Account", account, ["root_type", "is_group", "company"], as_dict=True)

	if not detail or detail.company != company:
		frappe.throw(
			_("{0} {1} does not belong to {2}.").format(label, frappe.bold(account), frappe.bold(company)),
			title=_("Account Belongs to Another Company"),
		)

	if detail.is_group:
		frappe.throw(
			_("{0} {1} is a group account. Pick a ledger account that can be posted to.").format(
				label, frappe.bold(account)
			),
			title=_("Group Account Selected"),
		)

	if detail.root_type != root_type:
		frappe.throw(
			_("{0} {1} is {2}, but it has to be {3}.").format(
				label, frappe.bold(account), frappe.bold(_(detail.root_type)), frappe.bold(_(root_type))
			),
			title=_("Wrong Account Type"),
		)


def validate_bank_account(account, *, company, loan_account, label):
	"""The one rule every bank account in this app is held to.

	Used for both fields on the facility and for whatever bank the payment
	dialog passes in, so a ledger that would be refused on the facility cannot
	slip in through the popup instead. Money must leave or arrive in a real
	bank or cash ledger of the borrowing company, and never in the loan account
	itself -- that would debit and credit the same ledger and post nothing.
	"""
	detail = frappe.db.get_value("Account", account, ["account_type", "is_group", "company"], as_dict=True)

	if not detail or detail.company != company:
		frappe.throw(
			_("{0} {1} does not belong to {2}.").format(label, frappe.bold(account), frappe.bold(company)),
			title=_("Account Belongs to Another Company"),
		)

	if detail.is_group:
		frappe.throw(
			_("{0} {1} is a group account. Pick a ledger account that can be posted to.").format(
				label, frappe.bold(account)
			),
			title=_("Group Account Selected"),
		)

	if detail.account_type not in BANK_ACCOUNT_TYPES:
		frappe.throw(
			_("{0} {1} is not a Bank or Cash account.").format(label, frappe.bold(account)),
			title=_("Not a Bank Account"),
		)

	if loan_account and account == loan_account:
		frappe.throw(
			_(
				"{0} cannot be the same account as the Loan Account. The entry would debit and credit the same ledger and post nothing."
			).format(label),
			title=_("Same Account on Both Sides"),
		)


def get_company_currency(company):
	"""Default currency of a company, or None when the company is not set yet."""
	return frappe.get_cached_value("Company", company, "default_currency") if company else None
