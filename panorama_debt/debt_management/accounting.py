# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

"""Journal Entry posting for Panorama Debt.

Every entry this module writes is inserted and submitted as the logged-in
user: no ``ignore_permissions``, no ``ignore_links``, and no commit. A user who
may not post a Journal Entry may not post a disbursement or a repayment
either, and the surrounding transaction is the caller's to commit or roll back.

Amounts arrive as :class:`~decimal.Decimal` and are rounded through
``schedule_engine.money`` so posted entries carry the same commercial rounding
as the schedule they came from.
"""

import frappe
from frappe import _
from frappe.utils import escape_html, formatdate, getdate, nowdate

from panorama_debt.debt_management.schedule_engine import ZERO, money

# Accounts that have to carry a party. A borrowing booked against a Payable
# account is owed to the provider, and a GL line without the party makes the
# supplier ledger disagree with the trial balance.
PARTY_ACCOUNT_TYPES = ("Payable",)


def validate_not_future(date, label):
	"""Refuse a posting date that has not happened yet.

	Back-dating within an open period is ordinary accounting; forward-dating is
	not. A future entry would sit in the ledger as though it had already
	happened, and every balance derived from it would be wrong until the date
	arrived.
	"""
	if date and getdate(date) > getdate(nowdate()):
		frappe.throw(
			_("Future-dated entries cannot be posted."),
			title=_("{0} Is in the Future").format(_(label)),
		)


def validate_settlement_date(posting_date, due_date, n):
	"""Check an instalment is being settled on a date it could have been paid.

	An instalment cannot be settled before it falls due: the money is not owed
	yet, and letting it through would put the repayment in a period the lender
	has not billed. The upper bound is today, for the reason above.
	"""
	validate_not_future(posting_date, "Posting Date")

	if posting_date and due_date and getdate(posting_date) < getdate(due_date):
		frappe.throw(
			_("Instalment {0} is due on {1}. It cannot be paid before the due date.").format(
				n, frappe.bold(formatdate(due_date))
			),
			title=_("Paid Before Due Date"),
		)


def get_account_details(accounts, company):
	"""Load once, for every distinct account a set of lines touches.

	One query instead of one per line, and it doubles as the existence check:
	an account that is not in the result does not belong to this company.
	"""
	rows = frappe.get_all(
		"Account",
		filters={"name": ("in", list(accounts)), "company": company},
		fields=["name", "root_type", "report_type", "account_type", "account_currency", "is_group"],
	)
	return {row.name: row for row in rows}


def get_party_for_provider(loan_provider):
	"""Supplier standing in for a loan provider on a Payable line.

	Loan Provider is this app's own master and means nothing to the general
	ledger, so a party line has to name the ERPNext Supplier the provider is
	mapped to. The mapping is optional on the master, which is why this fails
	loudly rather than posting a party-less Payable line.
	"""
	supplier = frappe.db.get_value("Loan Provider", loan_provider, "supplier")

	if not supplier:
		frappe.throw(
			_("{0} has no Supplier linked, so it cannot be used as the party on a payable account.").format(
				frappe.bold(escape_html(loan_provider))
			),
			title=_("Provider Has No Supplier"),
		)

	return supplier


def make_journal_entry(
	*,
	company,
	posting_date,
	voucher_type,
	reference_no,
	remark,
	loan_provider,
	lines,
):
	"""Post one balanced Journal Entry and return its name.

	``lines`` is a sequence of ``(account, debit, credit)`` with Decimal
	amounts. Lines that round to nothing on both sides are dropped rather than
	posted as empty rows, which is what happens to the interest leg of a 0%
	inter-corporate loan.

	The entry is submitted, not left in draft: a disbursement or a repayment is
	a fact by the time it reaches here, and a draft would leave the facility's
	figures disagreeing with the ledger.
	"""
	if not loan_provider:
		# Caught here rather than where a Payable line looks the party up:
		# a missed argument would otherwise surface much later, and only for
		# the facilities whose loan account happens to be a Payable one.
		frappe.throw(
			_("A Loan Provider is required to post this entry."),
			title=_("No Loan Provider"),
		)

	posting_date = getdate(posting_date)
	validate_not_future(posting_date, "Posting Date")

	rows = [
		(account, money(debit), money(credit))
		for account, debit, credit in lines
		if money(debit) != ZERO or money(credit) != ZERO
	]

	if not rows:
		frappe.throw(_("There is nothing to post: every line is zero."), title=_("Empty Entry"))

	details = get_account_details({account for account, _debit, _credit in rows}, company)
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	cost_center = frappe.get_cached_value("Company", company, "cost_center")

	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": voucher_type,
			"company": company,
			"posting_date": posting_date,
			# validate_cheque_info requires both of these on a Bank Entry, and
			# they are how the entry is traced back to the facility.
			"cheque_no": reference_no,
			"cheque_date": posting_date,
			"user_remark": remark,
		}
	)

	for account, debit, credit in rows:
		detail = details.get(account)

		if not detail:
			frappe.throw(
				_("Account {0} does not belong to {1}.").format(
					frappe.bold(escape_html(account)), frappe.bold(escape_html(company))
				),
				title=_("Account Not in Company"),
			)

		if detail.is_group:
			frappe.throw(
				_("Account {0} is a group account and cannot be posted to.").format(
					frappe.bold(escape_html(account))
				),
				title=_("Group Account"),
			)

		if detail.account_currency and detail.account_currency != company_currency:
			frappe.throw(
				_(
					"Account {0} is in {1}, but {2} books in {3}. Multi-currency borrowings are not supported."
				).format(
					frappe.bold(escape_html(account)),
					frappe.bold(escape_html(detail.account_currency)),
					frappe.bold(escape_html(company)),
					frappe.bold(escape_html(company_currency)),
				),
				title=_("Currency Mismatch"),
			)

		if detail.report_type == "Profit and Loss" and not cost_center:
			frappe.throw(
				_(
					"{0} has no default Cost Center, which {1} needs because it is a Profit and Loss account."
				).format(frappe.bold(escape_html(company)), frappe.bold(escape_html(account))),
				title=_("No Default Cost Center"),
			)

		row = {
			"account": account,
			# debit and credit are read-only and derived from these by
			# set_amounts_in_company_currency; the two agree because the
			# currency check above rules out any other rate.
			"debit_in_account_currency": float(debit),
			"credit_in_account_currency": float(credit),
			"cost_center": cost_center,
		}

		if detail.account_type in PARTY_ACCOUNT_TYPES:
			row["party_type"] = "Supplier"
			row["party"] = get_party_for_provider(loan_provider)

		entry.append("accounts", row)

	entry.insert()
	entry.submit()
	return entry.name


def cancel_journal_entries(names):
	"""Cancel the entries that are still submitted, and report which those were.

	Entries already cancelled are skipped rather than treated as an error: a
	facility and its schedule can both point at the same entry, and whichever
	is cancelled second must not fail because the first got there already.
	"""
	cancelled = []

	for name in [n for n in names if n]:
		if frappe.db.get_value("Journal Entry", name, "docstatus") != 1:
			continue

		frappe.get_doc("Journal Entry", name).cancel()
		cancelled.append(name)

	return cancelled


def journal_entry_reference_error(name, company):
	"""Why a user-supplied Journal Entry reference is unusable, or None if it is fine.

	Returned rather than thrown so a caller collecting many rows -- an upload,
	for one -- can report every bad reference at once instead of stopping at
	the first. The name is echoed back escaped because it came from a
	spreadsheet cell or a dialog, not from a Link field.
	"""
	if not name:
		return None

	entry = frappe.db.get_value("Journal Entry", name, ["docstatus", "company"], as_dict=True)
	safe_name = frappe.bold(escape_html(name))

	if not entry:
		return _("Journal Entry {0} does not exist.").format(safe_name)

	if entry.docstatus != 1:
		return _("Journal Entry {0} is not submitted.").format(safe_name)

	if entry.company != company:
		return _("Journal Entry {0} belongs to {1}, not to {2}.").format(
			safe_name, frappe.bold(escape_html(entry.company)), frappe.bold(escape_html(company))
		)

	return None
