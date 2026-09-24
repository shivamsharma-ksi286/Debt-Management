# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_address

# Statutory identifier formats. Each is checked only when a value is supplied,
# since an overseas lender or an informal inter-corporate counterparty may
# legitimately have none of them on file.
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
IFSC_PATTERN = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

IDENTIFIER_FORMATS = (
	("pan", PAN_PATTERN, "AAAAA0000A"),
	("gstin", GSTIN_PATTERN, "07AAAAA0000A1Z5"),
	("ifsc_code", IFSC_PATTERN, "ICIC0001234"),
)

# Which side of the books each per-company default account has to sit on.
# Money borrowed is a liability to this group; the interest it carries is an
# expense.
ACCOUNT_ROOT_TYPES = {
	"loan_account": "Liability",
	"interest_expense_account": "Expense",
}


class LoanProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from panorama_debt.debt_management.doctype.loan_provider_account.loan_provider_account import (
			LoanProviderAccount,
		)

		address: DF.SmallText | None
		branch_name: DF.Data | None
		company_accounts: DF.Table[LoanProviderAccount]
		contact_person: DF.Data | None
		email_id: DF.Data | None
		gstin: DF.Data | None
		ifsc_code: DF.Data | None
		is_active: DF.Check
		mobile_no: DF.Data | None
		notes: DF.SmallText | None
		pan: DF.Data | None
		provider_name: DF.Data
		provider_type: DF.Literal["Bank", "NBFC", "Inter-Corporate", "Related Party", "Other"]
		supplier: DF.Link | None
	# end: auto-generated types

	def validate(self):
		self.normalise_identifiers()
		self.validate_identifier_formats()
		self.validate_email()
		self.validate_company_accounts()

	def normalise_identifiers(self):
		"""Upper-case and trim PAN, GSTIN and IFSC before anything else reads them.

		The same bank gets keyed in as "icic0001234" and "ICIC0001234" by
		different people; normalising on the way in means the format checks
		below and any later lookup both see one canonical spelling.
		"""
		for fieldname, _pattern, _example in IDENTIFIER_FORMATS:
			value = (self.get(fieldname) or "").strip().upper()
			self.set(fieldname, value or None)

	def validate_identifier_formats(self):
		"""Reject a malformed PAN, GSTIN or IFSC, but never a blank one.

		A typo in any of these is silent and expensive -- it surfaces months
		later when a TDS return or a payment file is rejected -- so they are
		checked at entry. An empty field stays valid.
		"""
		for fieldname, pattern, example in IDENTIFIER_FORMATS:
			value = self.get(fieldname)
			if not value or pattern.match(value):
				continue

			label = _(self.meta.get_label(fieldname))
			frappe.throw(
				_("{0} {1} is not in a valid format. It should look like {2}.").format(
					label, frappe.bold(value), frappe.bold(example)
				),
				title=_("Invalid {0}").format(label),
			)

	def validate_email(self):
		"""Check the contact email is deliverable-looking, when one is given."""
		if self.email_id:
			validate_email_address(self.email_id, throw=True)

	def validate_company_accounts(self):
		"""Vet the per-company default accounts row by row.

		Every group company keeps its own chart of accounts, so one provider
		lending to two companies needs two different liability accounts. Each
		row is therefore checked three ways: the company appears only once, the
		account is a postable ledger on the expected side of the books, and the
		account actually belongs to that row's company. The third check is the
		one the link filter cannot make on its own, and it is the mistake that
		would otherwise post a facility into another company's ledger.
		"""
		rows_by_company = {}

		for row in self.company_accounts:
			if row.company in rows_by_company:
				frappe.throw(
					_("Row {0}: {1} is already listed in row {2}. Keep one row per company.").format(
						row.idx, frappe.bold(row.company), rows_by_company[row.company]
					),
					title=_("Duplicate Company"),
				)
			rows_by_company[row.company] = row.idx

			for fieldname, expected_root_type in ACCOUNT_ROOT_TYPES.items():
				self.validate_account(row, fieldname, expected_root_type)

	def validate_account(self, row, fieldname, expected_root_type):
		"""Check one account on one row is postable, on the right side, right company.

		Left deliberately tolerant of a blank: a provider can be set up before
		its accounts exist in the chart, and the Debt Facility simply gets no
		default in that case.
		"""
		account = row.get(fieldname)
		if not account:
			return

		root_type, is_group, account_company = frappe.db.get_value(
			"Account", account, ["root_type", "is_group", "company"]
		)
		label = _(row.meta.get_label(fieldname))

		if is_group:
			frappe.throw(
				_("Row {0}: {1} {2} is a group account. Pick a ledger account that can be posted to.").format(
					row.idx, label, frappe.bold(account)
				),
				title=_("Group Account Selected"),
			)

		if root_type != expected_root_type:
			frappe.throw(
				_("Row {0}: {1} {2} is {3}, but it has to be {4}.").format(
					row.idx,
					label,
					frappe.bold(account),
					frappe.bold(_(root_type)),
					frappe.bold(_(expected_root_type)),
				),
				title=_("Wrong Account Type"),
			)

		if account_company != row.company:
			frappe.throw(
				_("Row {0}: {1} {2} belongs to {3}, not to {4}.").format(
					row.idx,
					label,
					frappe.bold(account),
					frappe.bold(account_company),
					frappe.bold(row.company),
				),
				title=_("Account Belongs to Another Company"),
			)


def get_provider_accounts(loan_provider: str, company: str) -> dict:
	"""Default loan and interest accounts a provider keeps for one company.

	Keyed by the Debt Facility's own fieldnames so the result can be applied
	straight onto the facility. Missing values come back as empty strings
	rather than None, so assigning them clears a stale account instead of
	leaving a null behind. A provider with no row for the company yields two
	blanks and the user picks the accounts by hand.
	"""
	if not (loan_provider and company):
		return {"loan_liability_account": "", "interest_expense_account": ""}

	row = (
		frappe.db.get_value(
			"Loan Provider Account",
			{"parent": loan_provider, "parenttype": "Loan Provider", "company": company},
			["loan_account", "interest_expense_account"],
			as_dict=True,
		)
		or {}
	)

	return {
		"loan_liability_account": row.get("loan_account") or "",
		"interest_expense_account": row.get("interest_expense_account") or "",
	}
