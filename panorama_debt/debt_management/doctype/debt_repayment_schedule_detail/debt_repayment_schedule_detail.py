# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import flt


class DebtRepaymentScheduleDetail(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		closing_principal: DF.Currency
		due_date: DF.Date
		instalment_amount: DF.Currency
		instalment_number: DF.Int
		interest_amount: DF.Currency
		other_charges: DF.Currency
		outstanding_principal: DF.Currency
		paid_amount: DF.Currency
		paid_date: DF.Date | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		payment_entry: DF.Link | None
		payment_status: DF.Literal["Unpaid", "Paid", "Partially Paid", "Overdue", "Waived"]
		principal_amount: DF.Currency
		remarks: DF.SmallText | None
	# end: auto-generated types

	# Rows are produced by the schedule engine and validated by the parent
	# Debt Repayment Schedule, which is the only place that can see the whole
	# table and check it reconciles and closes on zero.

	def paid_interest(self):
		"""Interest actually settled on this instalment, in money.

		A part payment is appropriated to interest before principal, which is
		how banks apply receipts: the lender takes its earnings first and only
		the remainder reduces the balance. A waived instalment returns zero --
		it is settled, but not with money, so it must not inflate what the
		group has paid.
		"""
		if self.payment_status == "Paid":
			return flt(self.interest_amount)

		if self.payment_status == "Partially Paid":
			return min(flt(self.paid_amount), flt(self.interest_amount))

		return 0.0

	def paid_principal(self):
		"""Principal actually repaid by this instalment, in money.

		Whatever a part payment leaves after interest has been covered, capped
		at the principal due so an overpayment cannot repay more than the row
		carries.
		"""
		if self.payment_status == "Paid":
			return flt(self.principal_amount)

		if self.payment_status == "Partially Paid":
			remainder = flt(self.paid_amount) - self.paid_interest()
			return max(0.0, min(remainder, flt(self.principal_amount)))

		return 0.0
