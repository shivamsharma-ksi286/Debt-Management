"""Integration tests for the Debt Facility Summary report."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from panorama_debt.debt_management.report.debt_facility_summary.debt_facility_summary import execute

COMPANY = "Panorama Group"
LOAN_ACCOUNT = "Secured Loans - PG"
INTEREST_ACCOUNT = "Interest Expense - PG"
BANK_ACCOUNT = "ICICI Bank A/c - PG"

EXPECTED_COLUMNS = [
	"name",
	"loan_provider",
	"company",
	"status",
	"currency",
	"total_payable",
	"total_interest_payable",
	"outstanding_principal",
	"total_principal_paid",
	"total_interest_paid",
	"next_due_date",
	"next_due_amount",
]


class TestDebtFacilitySummary(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.provider = cls.ensure_provider("ICICI Bank Ltd (Report Test)", "Bank")
		cls.icd_provider = cls.ensure_provider("Group Co (Report Test)", "Inter-Corporate")

	@classmethod
	def ensure_provider(cls, name, provider_type):
		if frappe.db.exists("Loan Provider", name):
			return name
		return (
			frappe.get_doc(
				{
					"doctype": "Loan Provider",
					"provider_name": name,
					"provider_type": provider_type,
					"company_accounts": [
						{
							"company": COMPANY,
							"loan_account": LOAN_ACCOUNT,
							"interest_expense_account": INTEREST_ACCOUNT,
						}
					],
				}
			)
			.insert()
			.name
		)

	def make_facility(self, *, submit=True, **overrides):
		values = {
			"doctype": "Debt Facility",
			"company": COMPANY,
			"loan_provider": self.provider,
			"loan_type": "Term Loan",
			"sanction_date": "2025-08-01",
			"sanctioned_amount": 1000000,
			"disbursed_amount": 1000000,
			"disbursement_date": "2025-08-05",
			"rate_of_interest": 12.0,
			"number_of_instalments": 12,
			"repayment_method": "Equal Principal",
			"repayment_frequency": "Monthly",
			"repayment_start_date": "2025-08-31",
			"loan_liability_account": LOAN_ACCOUNT,
			"interest_expense_account": INTEREST_ACCOUNT,
			"bank_account": BANK_ACCOUNT,
			"repayment_bank_account": BANK_ACCOUNT,
			"disbursement_entry_posted": "Yes",
		}
		values.update(overrides)
		facility = frappe.get_doc(values).insert()
		if submit:
			facility.submit()
		return facility

	def names(self, filters=None):
		_columns, data = execute(filters or {})
		return [row.name for row in data]

	def test_columns_are_in_the_agreed_order(self):
		columns, _data = execute({})
		self.assertEqual([c["fieldname"] for c in columns], EXPECTED_COLUMNS)

	def test_the_currency_column_is_hidden_and_drives_the_money_columns(self):
		columns, _data = execute({})
		by_name = {c["fieldname"]: c for c in columns}
		self.assertTrue(by_name["currency"]["hidden"])
		for fieldname in ("total_payable", "outstanding_principal", "next_due_amount"):
			self.assertEqual(by_name[fieldname]["fieldtype"], "Currency")
			self.assertEqual(by_name[fieldname]["options"], "currency")

	def test_each_row_carries_its_company_currency(self):
		facility = self.make_facility()
		_columns, data = execute({"status": "Active"})
		row = next(r for r in data if r.name == facility.name)
		self.assertEqual(row.currency, frappe.get_cached_value("Company", COMPANY, "default_currency"))

	def test_a_draft_facility_is_excluded(self):
		draft = self.make_facility(submit=False)
		self.assertNotIn(draft.name, self.names({}))

	def test_a_revolving_facility_is_excluded(self):
		cc = self.make_facility(
			loan_type="Cash Credit", sanctioned_limit=500000, disbursement_entry_posted="Yes"
		)
		self.assertNotIn(cc.name, self.names({}))

	def test_a_submitted_term_facility_is_included(self):
		facility = self.make_facility()
		self.assertIn(facility.name, self.names({"status": "Active"}))

	def test_company_filter(self):
		facility = self.make_facility()
		self.assertIn(facility.name, self.names({"company": COMPANY}))
		self.assertNotIn(facility.name, self.names({"company": "Nonexistent Co"}))

	def test_loan_provider_filter(self):
		facility = self.make_facility()
		other = self.make_facility(loan_provider=self.icd_provider)
		names = self.names({"loan_provider": self.provider})
		self.assertIn(facility.name, names)
		self.assertNotIn(other.name, names)

	def test_provider_type_filter(self):
		bank = self.make_facility()
		icd = self.make_facility(loan_provider=self.icd_provider)
		names = self.names({"provider_type": "Inter-Corporate"})
		self.assertIn(icd.name, names)
		self.assertNotIn(bank.name, names)

	def test_loan_type_filter(self):
		term = self.make_facility(loan_type="Term Loan")
		wctl = self.make_facility(loan_type="Working Capital Term Loan")
		names = self.names({"loan_type": "Working Capital Term Loan"})
		self.assertIn(wctl.name, names)
		self.assertNotIn(term.name, names)

	def test_status_filter(self):
		facility = self.make_facility()
		self.assertIn(facility.name, self.names({"status": "Active"}))
		self.assertNotIn(facility.name, self.names({"status": "Closed"}))

	def test_in_scope_filter(self):
		out = self.make_facility(in_scope=0)
		self.assertNotIn(out.name, self.names({"in_scope": 1}))
		self.assertIn(out.name, self.names({"in_scope": 0}))

	def test_next_due_up_to_filter(self):
		facility = self.make_facility()
		facility.db_set("next_due_date", "2026-01-31")
		self.assertIn(facility.name, self.names({"next_due_up_to": "2026-02-28"}))
		self.assertNotIn(facility.name, self.names({"next_due_up_to": "2025-12-31"}))

	def test_a_facility_with_no_due_date_is_excluded_by_next_due_up_to(self):
		facility = self.make_facility()
		facility.db_set("next_due_date", None)
		self.assertNotIn(facility.name, self.names({"next_due_up_to": "2030-01-01"}))

	def test_soonest_due_first_and_undated_last(self):
		early = self.make_facility()
		late = self.make_facility()
		undated = self.make_facility()
		early.db_set("next_due_date", "2026-01-31")
		late.db_set("next_due_date", "2026-06-30")
		undated.db_set("next_due_date", None)

		_columns, data = execute({"status": "Active"})
		order = [r.name for r in data if r.name in (early.name, late.name, undated.name)]
		self.assertEqual(order, [early.name, late.name, undated.name])

	def test_outstanding_is_reported_per_facility(self):
		facility = self.make_facility()
		_columns, data = execute({"status": "Active"})
		row = next(r for r in data if r.name == facility.name)
		self.assertEqual(flt(row.outstanding_principal), 1000000.0)
