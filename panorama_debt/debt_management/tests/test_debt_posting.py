"""Integration tests for the Journal Entries this app posts.

These run against a real site: they submit facilities and schedules, post and
cancel Journal Entries, and read the resulting GL lines back. The figures come
from the client's ICICI working-capital term loan, so a change that quietly
alters what lands in the ledger fails here.

Test facility: INR 1,95,00,000 sanctioned and disbursed, 9% (I-EBLR 8.40% +
0.60%), 60 monthly instalments including a 12-month interest-servicing
moratorium, repaid interest-only then equal principal. Repayment starts
31-08-2025, so instalment 13 (31-08-2026) has fallen due and instalment 14
(30-09-2026) has not.
"""

import datetime

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate

COMPANY = "Panorama Group"
LOAN_ACCOUNT = "Secured Loans - PG"
INTEREST_ACCOUNT = "Interest Expense - PG"
BANK_ACCOUNT = "ICICI Bank A/c - PG"
# The fixture repays from the same ledger it was disbursed into. They are
# separate fields now, and the payment path reads only the repayment one.

PRINCIPAL = 19500000.00
MORATORIUM_INTEREST = 146250.00
PRINCIPAL_INSTALMENT = 406250.00


class DebtPostingTestCase(IntegrationTestCase):
	"""Shared fixtures. Everything rolls back when the class finishes."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.provider = cls.ensure_provider()

	@classmethod
	def ensure_provider(cls):
		name = "ICICI Bank Ltd (Test)"
		if frappe.db.exists("Loan Provider", name):
			return name

		return (
			frappe.get_doc(
				{
					"doctype": "Loan Provider",
					"provider_name": name,
					"provider_type": "Bank",
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

	def make_facility(self, **overrides):
		"""The test facility, submitted and ready to schedule."""
		values = {
			"doctype": "Debt Facility",
			"company": COMPANY,
			"loan_provider": self.provider,
			"loan_type": "Working Capital Term Loan",
			"purpose": "Integration test",
			"sanction_date": "2025-08-01",
			"sanctioned_amount": PRINCIPAL,
			"disbursed_amount": PRINCIPAL,
			"disbursement_date": "2025-08-05",
			"interest_rate_type": "Floating",
			"benchmark": "I-EBLR",
			"benchmark_rate": 8.40,
			"spread": 0.60,
			"rate_of_interest": 9.0,
			"tenure_months": 60,
			"number_of_instalments": 60,
			"moratorium_months": 12,
			"moratorium_type": "Interest Servicing",
			"repayment_method": "Interest Only then Equal Principal",
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
		facility.submit()
		return facility

	def make_schedule(self, facility):
		schedule = frappe.get_doc(
			{
				"doctype": "Debt Repayment Schedule",
				"company": COMPANY,
				"loan_provider": facility.loan_provider,
				"loan": facility.name,
				"posting_date": "2025-08-01",
			}
		)
		schedule.insert()
		schedule.generate_schedule()
		schedule.save()
		schedule.submit()
		return schedule

	def gl_lines(self, entry_name):
		"""Journal Entry lines as {account: (debit, credit)}."""
		entry = frappe.get_doc("Journal Entry", entry_name)
		return entry, {row.account: (flt(row.debit), flt(row.credit)) for row in entry.accounts}

	def first_instalment_due_after_today(self, schedule):
		"""The earliest instalment that has not fallen due yet.

		Found by date rather than by number: which instalment is still in the
		future depends on when the suite is run, and hard-coding one would turn
		this into a test that passes only in a particular month.
		"""
		today = getdate(nowdate())
		return next(row for row in schedule.repayment_schedule if getdate(row.due_date) > today)


class TestDisbursementEntry(DebtPostingTestCase):
	def test_posts_the_disbursement_on_submit(self):
		facility = self.make_facility(disbursement_entry_posted="No")
		facility.reload()

		self.assertTrue(facility.disbursement_journal_entry)
		self.assertEqual(facility.disbursement_entry_posted, "Yes")

		entry, lines = self.gl_lines(facility.disbursement_journal_entry)
		self.assertEqual(entry.voucher_type, "Bank Entry")
		self.assertEqual(entry.cheque_no, facility.name)
		self.assertEqual(getdate(entry.posting_date), getdate("2025-08-05"))
		self.assertEqual(lines[BANK_ACCOUNT], (PRINCIPAL, 0.0))
		self.assertEqual(lines[LOAN_ACCOUNT], (0.0, PRINCIPAL))

	def test_posts_nothing_when_already_booked(self):
		facility = self.make_facility(disbursement_entry_posted="Yes")
		facility.reload()
		self.assertFalse(facility.disbursement_journal_entry)

	def test_cancelling_the_facility_cancels_the_disbursement(self):
		facility = self.make_facility(disbursement_entry_posted="No")
		facility.reload()
		entry = facility.disbursement_journal_entry

		facility.cancel()
		facility.reload()

		self.assertEqual(facility.status, "Cancelled")
		self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 2)


class TestRepaymentEntries(DebtPostingTestCase):
	def setUp(self):
		super().setUp()
		self.facility = self.make_facility()
		self.schedule = self.make_schedule(self.facility)

	def test_instalment_one_is_interest_only(self):
		"""Inside the moratorium only interest is billed, so no principal line."""
		entry_name = self.schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")
		entry, lines = self.gl_lines(entry_name)

		self.assertEqual(entry.voucher_type, "Bank Entry")
		self.assertEqual(lines[INTEREST_ACCOUNT], (MORATORIUM_INTEREST, 0.0))
		self.assertEqual(lines[BANK_ACCOUNT], (0.0, MORATORIUM_INTEREST))
		self.assertNotIn(LOAN_ACCOUNT, lines, "a zero principal line must not be posted")

	def test_instalment_thirteen_repays_principal_and_interest(self):
		"""The first instalment after the moratorium carries both legs."""
		entry_name = self.schedule.make_payment_entry(instalment_number=13, posting_date="2026-08-31")
		entry, lines = self.gl_lines(entry_name)

		self.assertEqual(lines[LOAN_ACCOUNT], (PRINCIPAL_INSTALMENT, 0.0))
		self.assertEqual(lines[INTEREST_ACCOUNT], (MORATORIUM_INTEREST, 0.0))
		self.assertEqual(lines[BANK_ACCOUNT], (0.0, PRINCIPAL_INSTALMENT + MORATORIUM_INTEREST))
		self.assertEqual(flt(entry.total_debit), PRINCIPAL_INSTALMENT + MORATORIUM_INTEREST)

	def test_paying_marks_the_row_and_moves_the_facility(self):
		self.schedule.make_payment_entry(instalment_number=13, posting_date="2026-08-31")
		self.schedule.reload()
		self.facility.reload()

		row = self.schedule.repayment_schedule[12]
		self.assertEqual(row.payment_status, "Paid")
		self.assertEqual(row.entry_posted_by_system, 1)
		self.assertEqual(flt(row.paid_amount), PRINCIPAL_INSTALMENT + MORATORIUM_INTEREST)
		self.assertEqual(flt(self.facility.outstanding_principal), PRINCIPAL - PRINCIPAL_INSTALMENT)

	def test_the_reference_defaults_to_the_schedule_and_instalment(self):
		entry_name = self.schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")
		entry, _lines = self.gl_lines(entry_name)
		self.assertEqual(entry.cheque_no, f"{self.schedule.name}/1")

	def test_a_supplied_reference_is_used(self):
		entry_name = self.schedule.make_payment_entry(
			instalment_number=1, posting_date="2025-08-31", reference_no="UTR-9931"
		)
		entry, _lines = self.gl_lines(entry_name)
		self.assertEqual(entry.cheque_no, "UTR-9931")


class TestRepaymentBankAccount(DebtPostingTestCase):
	"""Payments credit the repayment bank, and never silently fall back."""

	def test_payment_credits_the_repayment_bank_not_the_receipt_bank(self):
		other = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "HDFC Repayment A/c",
				"company": COMPANY,
				"parent_account": "Bank Accounts - PG",
				"account_type": "Bank",
				"root_type": "Asset",
				"is_group": 0,
			}
		).insert()

		facility = self.make_facility(repayment_bank_account=other.name)
		schedule = self.make_schedule(facility)
		entry_name = schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")
		_entry, lines = self.gl_lines(entry_name)

		self.assertEqual(lines[other.name], (0.0, MORATORIUM_INTEREST))
		self.assertNotIn(BANK_ACCOUNT, lines, "the receipt bank must not be touched by a repayment")

	def test_no_repayment_bank_and_none_passed_is_refused(self):
		facility = self.make_facility()
		schedule = self.make_schedule(facility)
		facility.db_set("repayment_bank_account", None)

		with self.assertRaises(frappe.ValidationError) as caught:
			schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")

		self.assertIn("No bank account to pay from", str(caught.exception))

	def test_a_bank_passed_in_overrides_the_facility_default(self):
		facility = self.make_facility()
		schedule = self.make_schedule(facility)
		cash = frappe.db.get_value(
			"Account", {"company": COMPANY, "account_type": "Cash", "is_group": 0}, "name"
		)
		entry_name = schedule.make_payment_entry(
			instalment_number=1, posting_date="2025-08-31", bank_account=cash
		)
		_entry, lines = self.gl_lines(entry_name)
		self.assertEqual(lines[cash], (0.0, MORATORIUM_INTEREST))

	def test_the_loan_account_is_refused_as_a_bank(self):
		facility = self.make_facility()
		schedule = self.make_schedule(facility)

		with self.assertRaises(frappe.ValidationError) as caught:
			schedule.make_payment_entry(
				instalment_number=1, posting_date="2025-08-31", bank_account=LOAN_ACCOUNT
			)

		self.assertIn("not a Bank or Cash", str(caught.exception))

	def test_it_can_be_changed_on_a_submitted_facility(self):
		"""allow_on_submit: EMI mandates move between banks mid-loan."""
		facility = self.make_facility()
		cash = frappe.db.get_value(
			"Account", {"company": COMPANY, "account_type": "Cash", "is_group": 0}, "name"
		)
		facility.repayment_bank_account = cash
		facility.save()
		facility.reload()
		self.assertEqual(facility.repayment_bank_account, cash)

	def test_an_invalid_bank_is_still_refused_on_update_after_submit(self):
		"""validate() does not run on this path; before_update_after_submit does."""
		facility = self.make_facility()
		facility.repayment_bank_account = LOAN_ACCOUNT

		with self.assertRaises(frappe.ValidationError) as caught:
			facility.save()

		self.assertIn("not a Bank or Cash", str(caught.exception))


class TestAccountOverrides(DebtPostingTestCase):
	"""The payment dialog may override every ledger, within the same rules.

	A facility gets reclassified, or one month's interest is booked to a
	different head. The override still has to be a postable ledger of the
	borrowing company on the correct side of the books.
	"""

	def make_account(self, name, root_type, parent, account_type=None):
		return (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": name,
					"company": COMPANY,
					"parent_account": parent,
					"root_type": root_type,
					"account_type": account_type or "",
					"is_group": 0,
				}
			)
			.insert()
			.name
		)

	def setUp(self):
		super().setUp()
		self.facility = self.make_facility()
		self.schedule = self.make_schedule(self.facility)

	def test_overrides_are_posted_instead_of_the_facility_ledgers(self):
		loan = self.make_account("Unsecured Loans Alt", "Liability", "Loans (Liabilities) - PG")
		interest = self.make_account("Finance Cost Alt", "Expense", "Indirect Expenses - PG")
		bank = self.make_account("HDFC Pay A/c", "Asset", "Bank Accounts - PG", "Bank")

		entry_name = self.schedule.make_payment_entry(
			instalment_number=13,
			posting_date="2026-08-31",
			loan_account=loan,
			interest_account=interest,
			bank_account=bank,
		)
		_entry, lines = self.gl_lines(entry_name)

		self.assertEqual(lines[loan], (PRINCIPAL_INSTALMENT, 0.0))
		self.assertEqual(lines[interest], (MORATORIUM_INTEREST, 0.0))
		self.assertEqual(lines[bank], (0.0, PRINCIPAL_INSTALMENT + MORATORIUM_INTEREST))
		for untouched in (LOAN_ACCOUNT, INTEREST_ACCOUNT, BANK_ACCOUNT):
			self.assertNotIn(untouched, lines)

	def test_the_facility_ledgers_are_used_when_nothing_is_overridden(self):
		entry_name = self.schedule.make_payment_entry(instalment_number=13, posting_date="2026-08-31")
		_entry, lines = self.gl_lines(entry_name)
		self.assertEqual(lines[LOAN_ACCOUNT], (PRINCIPAL_INSTALMENT, 0.0))
		self.assertEqual(lines[INTEREST_ACCOUNT], (MORATORIUM_INTEREST, 0.0))

	def test_an_expense_account_is_refused_as_the_loan_account(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.schedule.make_payment_entry(
				instalment_number=13, posting_date="2026-08-31", loan_account=INTEREST_ACCOUNT
			)
		self.assertIn("has to be", str(caught.exception))
		self.assertIn("Liability", str(caught.exception))

	def test_a_liability_account_is_refused_as_the_interest_account(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.schedule.make_payment_entry(
				instalment_number=13, posting_date="2026-08-31", interest_account=LOAN_ACCOUNT
			)
		self.assertIn("Expense", str(caught.exception))

	def test_a_group_account_is_refused(self):
		group = frappe.db.get_value(
			"Account", {"company": COMPANY, "is_group": 1, "root_type": "Liability"}, "name"
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			self.schedule.make_payment_entry(
				instalment_number=13, posting_date="2026-08-31", loan_account=group
			)
		self.assertIn("group account", str(caught.exception))


class TestDateRejections(DebtPostingTestCase):
	def setUp(self):
		super().setUp()
		self.facility = self.make_facility()
		self.schedule = self.make_schedule(self.facility)

	def test_an_instalment_cannot_be_paid_before_it_falls_due(self):
		row = self.first_instalment_due_after_today(self.schedule)

		with self.assertRaises(frappe.ValidationError) as caught:
			self.schedule.make_payment_entry(instalment_number=row.instalment_number, posting_date=nowdate())

		self.assertIn("before the due date", str(caught.exception))

	def test_a_payment_cannot_be_posted_in_the_future(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.schedule.make_payment_entry(instalment_number=1, posting_date=add_days(nowdate(), 1))

		self.assertIn("Future-dated entries cannot be posted", str(caught.exception))

	def test_a_settled_instalment_cannot_be_paid_again(self):
		self.schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")

		with self.assertRaises(frappe.ValidationError) as caught:
			self.schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")

		self.assertIn("no payment can be posted", str(caught.exception))


class TestReversal(DebtPostingTestCase):
	def setUp(self):
		super().setUp()
		self.facility = self.make_facility()
		self.schedule = self.make_schedule(self.facility)

	def test_reversing_cancels_the_entry_and_resets_the_row(self):
		entry = self.schedule.make_payment_entry(instalment_number=13, posting_date="2026-08-31")

		self.assertEqual(self.schedule.reverse_payment(instalment_number=13), entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 2)

		self.schedule.reload()
		row = self.schedule.repayment_schedule[12]
		self.assertEqual(row.payment_status, "Unpaid")
		self.assertIsNone(row.payment_entry)
		self.assertEqual(row.entry_posted_by_system, 0)
		self.assertEqual(flt(row.paid_amount), 0.0)

		self.facility.reload()
		self.assertEqual(flt(self.facility.outstanding_principal), PRINCIPAL)

	def test_reversing_a_hand_recorded_payment_cancels_nothing(self):
		self.schedule.mark_instalment_paid(
			instalment_number=1, paid_date="2025-08-31", paid_amount=MORATORIUM_INTEREST
		)

		self.assertIsNone(self.schedule.reverse_payment(instalment_number=1))

		self.schedule.reload()
		self.assertEqual(self.schedule.repayment_schedule[0].payment_status, "Unpaid")

	def test_an_unpaid_instalment_has_nothing_to_reverse(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.schedule.reverse_payment(instalment_number=20)

		self.assertIn("nothing to reverse", str(caught.exception).lower())


class TestScheduleCancellation(DebtPostingTestCase):
	def test_cancelling_reverses_only_what_this_app_posted(self):
		facility = self.make_facility()
		schedule = self.make_schedule(facility)

		posted = schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")

		# A second instalment settled by hand against an entry someone else made.
		by_hand = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": COMPANY,
				"posting_date": "2025-09-30",
				"cheque_no": "MANUAL-1",
				"cheque_date": "2025-09-30",
				"accounts": [
					{
						"account": LOAN_ACCOUNT,
						"debit_in_account_currency": 1000,
						"cost_center": frappe.get_cached_value("Company", COMPANY, "cost_center"),
					},
					{
						"account": BANK_ACCOUNT,
						"credit_in_account_currency": 1000,
						"cost_center": frappe.get_cached_value("Company", COMPANY, "cost_center"),
					},
				],
			}
		).insert()
		by_hand.submit()

		schedule.reload()
		schedule.mark_instalment_paid(
			instalment_number=2,
			paid_date="2025-09-30",
			paid_amount=MORATORIUM_INTEREST,
			payment_entry=by_hand.name,
		)

		schedule.reload()
		schedule.cancel()

		self.assertEqual(frappe.db.get_value("Journal Entry", posted, "docstatus"), 2)
		self.assertEqual(
			frappe.db.get_value("Journal Entry", by_hand.name, "docstatus"),
			1,
			"an entry this app did not post must survive the cancellation",
		)

	def test_cancelling_restores_the_facility_to_its_disbursed_position(self):
		facility = self.make_facility()
		schedule = self.make_schedule(facility)
		schedule.make_payment_entry(instalment_number=13, posting_date="2026-08-31")

		schedule.reload()
		schedule.cancel()
		facility.reload()

		self.assertEqual(flt(facility.outstanding_principal), PRINCIPAL)
		self.assertEqual(flt(facility.total_principal_paid), 0.0)
		self.assertIsNone(facility.next_due_date)


class TestPermissions(DebtPostingTestCase):
	"""A user who may read the schedule must not be able to post from it.

	run_doc_method only checks read permission, so the write check each
	mutating method makes for itself is the only thing standing between a
	read-only user and the general ledger.
	"""

	ROLE = "Debt Read Only Test"
	USER = "debt.readonly.test@example.com"

	def setUp(self):
		super().setUp()
		self.facility = self.make_facility()
		self.schedule = self.make_schedule(self.facility)

		if not frappe.db.exists("Role", self.ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": self.ROLE}).insert()

		for doctype in ("Debt Repayment Schedule", "Debt Facility"):
			if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": self.ROLE}):
				frappe.get_doc(
					{
						"doctype": "Custom DocPerm",
						"parent": doctype,
						"parenttype": "DocType",
						"parentfield": "permissions",
						"role": self.ROLE,
						"permlevel": 0,
						"read": 1,
						"write": 0,
						"submit": 0,
						"cancel": 0,
					}
				).insert()

		if not frappe.db.exists("User", self.USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": self.USER,
					"first_name": "Debt Reader",
					"send_welcome_email": 0,
					"roles": [{"role": self.ROLE}],
				}
			).insert()

		frappe.clear_cache(user=self.USER)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_a_read_only_user_may_read_but_not_post(self):
		frappe.set_user(self.USER)
		schedule = frappe.get_doc("Debt Repayment Schedule", self.schedule.name)
		self.assertTrue(schedule.repayment_schedule, "the user should be able to read the schedule")

		with self.assertRaises(frappe.PermissionError):
			schedule.make_payment_entry(instalment_number=1, posting_date="2025-08-31")

	def test_a_read_only_user_cannot_reverse_or_mark_paid(self):
		frappe.set_user(self.USER)
		schedule = frappe.get_doc("Debt Repayment Schedule", self.schedule.name)

		with self.assertRaises(frappe.PermissionError):
			schedule.mark_instalment_paid(
				instalment_number=1, paid_date="2025-08-31", paid_amount=MORATORIUM_INTEREST
			)

		with self.assertRaises(frappe.PermissionError):
			schedule.reverse_payment(instalment_number=1)


class TestScheduleFigures(DebtPostingTestCase):
	"""The generated schedule must still match the sanction letter annexure."""

	def test_matches_the_annexure(self):
		facility = self.make_facility()
		schedule = self.make_schedule(facility)
		rows = schedule.repayment_schedule

		self.assertEqual(len(rows), 60)
		self.assertEqual(getdate(rows[0].due_date), datetime.date(2025, 8, 31))
		self.assertEqual(getdate(rows[12].due_date), datetime.date(2026, 8, 31))
		self.assertEqual(getdate(rows[13].due_date), datetime.date(2026, 9, 30))

		for row in rows[:12]:
			self.assertEqual(flt(row.interest_amount), MORATORIUM_INTEREST)
			self.assertEqual(flt(row.principal_amount), 0.0)

		self.assertEqual(flt(rows[12].principal_amount), PRINCIPAL_INSTALMENT)
		self.assertEqual(flt(rows[12].interest_amount), MORATORIUM_INTEREST)
		self.assertEqual(flt(rows[13].interest_amount), 143203.13)
		self.assertEqual(flt(rows[-1].closing_principal), 0.0)
