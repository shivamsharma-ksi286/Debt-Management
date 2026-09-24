# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class LoanProviderAccount(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		interest_expense_account: DF.Link | None
		loan_account: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
	# end: auto-generated types

	# Rows are validated by the parent Loan Provider, which is the only place
	# that can see the whole table and check one company appears once.
	pass
