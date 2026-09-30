"""Unit tests for the amortisation engine.

Plain unittest.TestCase, not FrappeTestCase: the engine is pure arithmetic with
no DocType coupling, so these run without a site or a database.

The first test is the acceptance case -- a real ICICI Bank sanction letter from
the client -- and the generated schedule must match its annexure to the paisa.
"""

import unittest
from decimal import Decimal
from itertools import pairwise

from panorama_debt.debt_management.schedule_engine import (
	compute_emi,
	derive_balances,
	generate_schedule,
	get_due_dates,
	money,
	summarise_schedule,
)

# (instalment_number, outstanding_principal, principal_amount, interest_amount,
#  instalment_amount) exactly as printed in the sanction letter annexure.
ICICI_EXPECTED = [(n, 19500000.00, 0.00, 146250.00, 146250.00) for n in range(1, 13)] + [
	(13, 19500000.00, 406250.00, 146250.00, 552500.00),
	(14, 19093750.00, 406250.00, 143203.13, 549453.13),
	(15, 18687500.00, 406250.00, 140156.25, 546406.25),
	(16, 18281250.00, 406250.00, 137109.38, 543359.38),
	(17, 17875000.00, 406250.00, 134062.50, 540312.50),
	(18, 17468750.00, 406250.00, 131015.63, 537265.63),
	(19, 17062500.00, 406250.00, 127968.75, 534218.75),
	(20, 16656250.00, 406250.00, 124921.88, 531171.88),
	(21, 16250000.00, 406250.00, 121875.00, 528125.00),
]


class ScheduleAssertions(unittest.TestCase):
	"""Shared checks every generated schedule has to satisfy."""

	def assert_rows_match(self, schedule, expected):
		"""Compare rows against a sanction-letter table, exactly -- no tolerance."""
		for number, opening, principal, interest, instalment in expected:
			row = schedule[number - 1]
			self.assertEqual(row["instalment_number"], number)
			for field, want in (
				("outstanding_principal", opening),
				("principal_amount", principal),
				("interest_amount", interest),
				("instalment_amount", instalment),
			):
				self.assertEqual(
					row[field],
					want,
					f"instalment {number}, {field}: got {row[field]!r}, want {want!r}",
				)

	def assert_schedule_closes(self, schedule, principal):
		"""Principal must be fully repaid and the balance must land on 0.00."""
		repaid = sum((money(row["principal_amount"]) for row in schedule), Decimal("0.00"))
		self.assertEqual(repaid, money(principal))
		self.assertEqual(schedule[-1]["closing_principal"], 0.00)

	def assert_rows_are_continuous(self, schedule):
		"""Each row's closing balance must be the next row's opening balance."""
		for previous, current in pairwise(schedule):
			self.assertEqual(
				previous["closing_principal"],
				current["outstanding_principal"],
				f"balance breaks between instalment {previous['instalment_number']} "
				f"and {current['instalment_number']}",
			)


class TestCommercialRounding(ScheduleAssertions):
	def test_ties_round_away_from_zero(self):
		"""The spec's sanity check: 143203.125 -> .13, not banker's .12."""
		self.assertEqual(money(143203.125), Decimal("143203.13"))
		self.assertEqual(money("143203.125"), Decimal("143203.13"))
		self.assertEqual(money(-143203.125), Decimal("-143203.13"))
		self.assertEqual(money(0.005), Decimal("0.01"))


class TestICICITermLoan(ScheduleAssertions):
	"""Acceptance test -- real ICICI sanction letter.

	INR 1.95 crore at 9% for 60 monthly instalments, the first 12 being an
	interest-servicing moratorium, then equal principal over the remaining 48.
	"""

	principal = 19500000.00

	def setUp(self):
		self.schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=9.0,
			number_of_instalments=60,
			moratorium_months=12,
			moratorium_type="Interest Servicing",
			repayment_method="Interest Only then Equal Principal",
			repayment_frequency="Monthly",
		)

	def test_matches_sanction_letter_annexure(self):
		self.assert_rows_match(self.schedule, ICICI_EXPECTED)

	def test_runs_for_sixty_instalments(self):
		self.assertEqual(len(self.schedule), 60)

	def test_repays_principal_in_full_and_closes_at_zero(self):
		self.assert_schedule_closes(self.schedule, self.principal)
		self.assert_rows_are_continuous(self.schedule)

	def test_moratorium_repays_no_principal(self):
		self.assertTrue(all(row["principal_amount"] == 0.00 for row in self.schedule[:12]))
		self.assertTrue(all(row["principal_amount"] > 0.00 for row in self.schedule[12:]))

	def test_final_instalment_clears_the_balance(self):
		final = self.schedule[-1]
		self.assertEqual(final["instalment_number"], 60)
		self.assertEqual(final["outstanding_principal"], 406250.00)
		self.assertEqual(final["principal_amount"], 406250.00)
		self.assertEqual(final["closing_principal"], 0.00)

	def test_due_dates_follow_the_repayment_start_date(self):
		dated = generate_schedule(
			principal=self.principal,
			rate_of_interest=9.0,
			number_of_instalments=60,
			moratorium_months=12,
			moratorium_type="Interest Servicing",
			repayment_method="Interest Only then Equal Principal",
			repayment_frequency="Monthly",
			repayment_start_date="2026-01-31",
		)
		self.assertEqual(str(dated[0]["due_date"]), "2026-01-31")
		# February has no 31st, so it clamps -- and March recovers the 31st
		# because dates are anchored on the start date, never chained.
		self.assertEqual(str(dated[1]["due_date"]), "2026-02-28")
		self.assertEqual(str(dated[2]["due_date"]), "2026-03-31")
		self.assertEqual(str(dated[-1]["due_date"]), "2030-12-31")


class TestFiveYearEMI(ScheduleAssertions):
	"""Plain 5-year monthly EMI, no moratorium: INR 12,00,000 at 12%."""

	principal = 1200000.00

	def setUp(self):
		self.schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=12.0,
			number_of_instalments=60,
			repayment_method="Equal Instalment (EMI)",
			repayment_frequency="Monthly",
		)

	def test_emi_is_the_annuity_payment(self):
		self.assertEqual(compute_emi(self.principal, Decimal("0.01"), 60), Decimal("26693.34"))

	def test_matches_reference_amortisation(self):
		self.assert_rows_match(
			self.schedule,
			[
				(1, 1200000.00, 14693.34, 12000.00, 26693.34),
				(2, 1185306.66, 14840.27, 11853.07, 26693.34),
				(3, 1170466.39, 14988.68, 11704.66, 26693.34),
				(59, 52596.18, 26167.38, 525.96, 26693.34),
				(60, 26428.80, 26428.80, 264.29, 26693.09),
			],
		)

	def test_instalment_is_level_except_the_last(self):
		self.assertTrue(all(row["instalment_amount"] == 26693.34 for row in self.schedule[:59]))
		self.assertNotEqual(self.schedule[-1]["instalment_amount"], 26693.34)

	def test_interest_declines_every_period(self):
		interest = [row["interest_amount"] for row in self.schedule]
		self.assertEqual(interest, sorted(interest, reverse=True))

	def test_repays_principal_in_full_and_closes_at_zero(self):
		self.assertEqual(len(self.schedule), 60)
		self.assert_schedule_closes(self.schedule, self.principal)
		self.assert_rows_are_continuous(self.schedule)

	def test_totals_reconcile_with_the_rows(self):
		totals = summarise_schedule(self.schedule)
		self.assertEqual(totals["total_principal"], 1200000.00)
		self.assertEqual(totals["total_interest"], 401600.15)
		self.assertEqual(totals["total_payable"], 1601600.15)


class TestInterestFreeInterCorporateLoan(ScheduleAssertions):
	"""A genuinely 0% inter-corporate deposit: INR 1 crore over 24 instalments.

	The EMI formula is 0/0 at a zero rate, so it has to degenerate to P / n
	with no interest at all rather than blowing up.
	"""

	principal = 10000000.00

	def setUp(self):
		self.schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=0,
			number_of_instalments=24,
			repayment_method="Equal Instalment (EMI)",
			repayment_frequency="Monthly",
		)

	def test_charges_no_interest(self):
		self.assertTrue(all(row["interest_amount"] == 0.00 for row in self.schedule))
		self.assertEqual(summarise_schedule(self.schedule)["total_interest"], 0.00)

	def test_splits_principal_evenly_with_the_residual_on_the_last_row(self):
		self.assert_rows_match(
			self.schedule,
			[
				(1, 10000000.00, 416666.67, 0.00, 416666.67),
				(2, 9583333.33, 416666.67, 0.00, 416666.67),
				(24, 416666.59, 416666.59, 0.00, 416666.59),
			],
		)

	def test_repays_principal_in_full_and_closes_at_zero(self):
		self.assertEqual(len(self.schedule), 24)
		self.assert_schedule_closes(self.schedule, self.principal)
		self.assert_rows_are_continuous(self.schedule)

	def test_equal_principal_at_zero_percent_behaves_the_same(self):
		schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=0,
			number_of_instalments=24,
			repayment_method="Equal Principal",
			repayment_frequency="Monthly",
		)
		self.assert_schedule_closes(schedule, self.principal)
		self.assertEqual(summarise_schedule(schedule)["total_payable"], self.principal)


class TestOtherMethods(ScheduleAssertions):
	"""The remaining repayment methods, kept honest by the same invariants."""

	principal = 1000000.00

	def test_bullet_pays_interest_then_the_whole_principal_at_the_end(self):
		schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=12.0,
			number_of_instalments=12,
			repayment_method="Bullet",
			repayment_frequency="Monthly",
		)
		self.assertTrue(all(row["principal_amount"] == 0.00 for row in schedule[:11]))
		self.assertTrue(all(row["instalment_amount"] == 10000.00 for row in schedule[:11]))
		self.assertEqual(schedule[-1]["principal_amount"], self.principal)
		self.assert_schedule_closes(schedule, self.principal)

	def test_full_moratorium_capitalises_interest_into_the_principal(self):
		schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=12.0,
			number_of_instalments=15,
			moratorium_months=3,
			moratorium_type="Full Moratorium (Interest Capitalised)",
			repayment_method="Interest Only then EMI",
			repayment_frequency="Monthly",
		)
		# Nothing is billed during the moratorium; the balance grows at 1%/month.
		self.assertTrue(all(row["instalment_amount"] == 0.00 for row in schedule[:3]))
		self.assertEqual(schedule[0]["closing_principal"], 1010000.00)
		self.assertEqual(schedule[1]["closing_principal"], 1020100.00)
		self.assertEqual(schedule[2]["closing_principal"], 1030301.00)
		# The grossed-up balance, not the original principal, is what amortises.
		self.assertEqual(schedule[3]["outstanding_principal"], 1030301.00)
		self.assert_schedule_closes(schedule, 1030301.00)
		self.assert_rows_are_continuous(schedule)

	def test_quarterly_moratorium_is_counted_in_whole_periods(self):
		schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=12.0,
			number_of_instalments=20,
			moratorium_months=12,
			moratorium_type="Interest Servicing",
			repayment_method="Interest Only then Equal Principal",
			repayment_frequency="Quarterly",
			repayment_start_date="2026-03-31",
		)
		# 12 months of moratorium on a quarterly loan is 4 instalments, not 12.
		self.assertTrue(all(row["principal_amount"] == 0.00 for row in schedule[:4]))
		self.assertEqual(schedule[4]["principal_amount"], 62500.00)
		self.assertEqual(schedule[0]["interest_amount"], 30000.00)
		self.assertEqual(str(schedule[1]["due_date"]), "2026-06-30")
		self.assert_schedule_closes(schedule, self.principal)

	def test_manual_method_generates_nothing(self):
		self.assertEqual(
			generate_schedule(
				principal=self.principal,
				rate_of_interest=12.0,
				number_of_instalments=12,
				repayment_method="As per Bank Schedule (Manual)",
			),
			[],
		)


class TestInvalidInput(unittest.TestCase):
	def test_rejects_bad_terms(self):
		base = dict(
			principal=1000000.00,
			rate_of_interest=9.0,
			number_of_instalments=12,
			repayment_method="Equal Instalment (EMI)",
		)
		for label, overrides in (
			("no instalments", {"number_of_instalments": 0}),
			("no principal", {"principal": 0}),
			("negative rate", {"rate_of_interest": -1}),
			("unknown method", {"repayment_method": "Balloon"}),
			("unknown frequency", {"repayment_frequency": "Fortnightly"}),
			("moratorium swallows the loan", {"moratorium_months": 12}),
		):
			with self.subTest(label), self.assertRaises(ValueError):
				generate_schedule(**{**base, **overrides})

	def test_undated_schedule_still_generates(self):
		self.assertEqual(get_due_dates(None, 3, "Monthly"), [None, None, None])


class TestCapitalisedTotals(ScheduleAssertions):
	"""Total payable must be what is billed, not principal plus interest.

	Under a capitalising moratorium the accrued interest is rolled into the
	balance instead of being billed, and is repaid later as principal. Counting
	it in total payable as well would charge the group for it twice.
	"""

	principal = 1000000.00

	def setUp(self):
		self.schedule = generate_schedule(
			principal=self.principal,
			rate_of_interest=12.0,
			number_of_instalments=15,
			moratorium_months=3,
			moratorium_type="Full Moratorium (Interest Capitalised)",
			repayment_method="Interest Only then EMI",
			repayment_frequency="Monthly",
		)
		self.totals = summarise_schedule(self.schedule)

	def test_total_payable_is_the_sum_of_instalments(self):
		billed = round(sum(row["instalment_amount"] for row in self.schedule), 2)
		self.assertEqual(self.totals["total_payable"], billed)

	def test_capitalised_interest_is_not_billed_twice(self):
		# The three moratorium instalments bill nothing at all.
		self.assertEqual([row["instalment_amount"] for row in self.schedule[:3]], [0.0, 0.0, 0.0])
		naive = self.totals["total_principal"] + self.totals["total_interest"]
		self.assertLess(self.totals["total_payable"], naive)
		# The gap is exactly the interest that was rolled into the balance.
		capitalised = round(sum(row["interest_amount"] for row in self.schedule[:3]), 2)
		self.assertAlmostEqual(naive - self.totals["total_payable"], capitalised, places=2)

	def test_ordinary_schedule_totals_still_add_up(self):
		plain = generate_schedule(
			principal=self.principal,
			rate_of_interest=12.0,
			number_of_instalments=12,
			repayment_method="Equal Principal",
			repayment_frequency="Monthly",
		)
		totals = summarise_schedule(plain)
		self.assertAlmostEqual(
			totals["total_payable"], totals["total_principal"] + totals["total_interest"], places=2
		)


class TestDeriveBalances(ScheduleAssertions):
	"""Balances worked out from principal and interest, for rows we did not generate."""

	def test_derives_running_balances(self):
		rows = [
			{"principal_amount": 250000.00, "interest_amount": 7500.00},
			{"principal_amount": 250000.00, "interest_amount": 5625.00},
			{"principal_amount": 250000.00, "interest_amount": 3750.00},
			{"principal_amount": 250000.00, "interest_amount": 1875.00},
		]
		derived = derive_balances(rows, 1000000.00)
		self.assertEqual(
			[r["outstanding_principal"] for r in derived], [1000000.00, 750000.00, 500000.00, 250000.00]
		)
		self.assertEqual([r["closing_principal"] for r in derived], [750000.00, 500000.00, 250000.00, 0.00])
		self.assertEqual(
			[r["instalment_amount"] for r in derived], [257500.00, 255625.00, 253750.00, 251875.00]
		)

	def test_does_not_mutate_the_input(self):
		rows = [{"principal_amount": 100.00, "interest_amount": 1.00}]
		derive_balances(rows, 100.00)
		self.assertEqual(rows, [{"principal_amount": 100.00, "interest_amount": 1.00}])

	def test_other_charges_join_the_instalment(self):
		rows = [{"principal_amount": 100.00, "interest_amount": 1.00, "other_charges": 25.00}]
		self.assertEqual(derive_balances(rows, 100.00)[0]["instalment_amount"], 126.00)

	def test_capitalised_rows_grow_the_balance_and_bill_nothing(self):
		rows = [
			{"principal_amount": 0.00, "interest_amount": 10000.00},
			{"principal_amount": 0.00, "interest_amount": 10100.00},
			{"principal_amount": 1020100.00, "interest_amount": 10201.00},
		]
		derived = derive_balances(rows, 1000000.00, capitalised_periods=2)
		self.assertEqual([r["outstanding_principal"] for r in derived], [1000000.00, 1010000.00, 1020100.00])
		self.assertEqual([r["instalment_amount"] for r in derived][:2], [0.00, 0.00])
		self.assertEqual(derived[-1]["closing_principal"], 0.00)

	def test_a_row_repaying_principal_ends_the_moratorium_early(self):
		rows = [
			{"principal_amount": 0.00, "interest_amount": 1000.00},
			{"principal_amount": 50000.00, "interest_amount": 1010.00},
		]
		derived = derive_balances(rows, 100000.00, capitalised_periods=5)
		self.assertEqual(derived[0]["closing_principal"], 101000.00)
		# row 2 repays principal, so it is billed normally despite the window
		self.assertEqual(derived[1]["instalment_amount"], 51010.00)
		self.assertEqual(derived[1]["closing_principal"], 51000.00)


if __name__ == "__main__":
	unittest.main(verbosity=2)
