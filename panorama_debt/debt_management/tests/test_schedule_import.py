"""Unit tests for the spreadsheet parser.

Plain unittest.TestCase: parse_schedule_rows is pure, so these run without a
site or a database. The fixtures are deliberately ugly -- bank annexures arrive
with title blocks, reordered columns, rupee symbols and PDF line wraps, and the
parser's job is to survive all of it while reporting anything it cannot.
"""

import datetime
import unittest

from panorama_debt.debt_management.schedule_import import (
	normalise_heading,
	parse_amount,
	parse_date,
	parse_schedule_rows,
	parse_status,
)

TODAY = datetime.date(2026, 9, 24)
HEADER = ["Due Date", "Principal Amount", "Interest", "Status", "Payment Entry Reference"]


def parse(rows, *, dayfirst=True, today=TODAY, disbursement_date=None):
	return parse_schedule_rows(rows, dayfirst=dayfirst, today=today, disbursement_date=disbursement_date)


class TestHeadings(unittest.TestCase):
	def test_normalises_case_whitespace_and_parentheses(self):
		self.assertEqual(normalise_heading("  Principal (in Rupees) "), "principal")
		self.assertEqual(normalise_heading("Due\nDate"), "due date")
		self.assertEqual(normalise_heading("INTEREST AMOUNT"), "interest amount")

	def test_finds_the_header_below_a_title_block(self):
		rows = [
			["ICICI Bank Limited"],
			[],
			["Sanction Reference: ICIC-TL-0098231"],
			["Annexure I - Repayment Schedule"],
			HEADER,
			["31-08-2025", "0", "146250", "", ""],
		]
		parsed, errors = parse(rows)
		self.assertEqual(errors, [])
		self.assertEqual(len(parsed), 1)

	def test_accepts_the_banks_own_headings(self):
		rows = [
			["Instalment Date", "Principal (in Rupees)", "Interest (in Rupees)"],
			["31-08-2025", "0", "1,46,250.00"],
		]
		parsed, errors = parse(rows)
		self.assertEqual(errors, [])
		self.assertEqual(parsed[0]["interest_amount"], 146250.00)

	def test_columns_may_be_in_any_order_and_extras_are_ignored(self):
		rows = [
			["Sr No", "Interest", "Narration", "Due Date", "Principal Amount"],
			[1, "146250", "EMI", "31-08-2025", "0"],
		]
		parsed, errors = parse(rows)
		self.assertEqual(errors, [])
		self.assertEqual(parsed[0]["due_date"], datetime.date(2025, 8, 31))
		self.assertEqual(parsed[0]["interest_amount"], 146250.00)

	def test_gives_up_past_ten_non_blank_rows(self):
		rows = [[f"Preamble line {n}"] for n in range(12)] + [HEADER, ["31-08-2025", "0", "1"]]
		parsed, errors = parse(rows)
		self.assertEqual(parsed, [])
		self.assertIn("No heading row found", errors[0])

	def test_a_header_missing_a_required_column_is_not_a_header(self):
		parsed, errors = parse([["Due Date", "Narration"], ["31-08-2025", "x"]])
		self.assertEqual(parsed, [])
		self.assertIn("No heading row found", errors[0])


class TestDates(unittest.TestCase):
	def test_accepts_date_and_datetime_cells(self):
		self.assertEqual(parse_date(datetime.date(2025, 8, 31), True)[0], datetime.date(2025, 8, 31))
		self.assertEqual(
			parse_date(datetime.datetime(2025, 8, 31, 13, 5), True)[0], datetime.date(2025, 8, 31)
		)

	def test_accepts_excel_serial_numbers(self):
		# 45900 is 31-08-2025 on Excel's 1899-12-30 epoch.
		self.assertEqual(parse_date(45900, True)[0], datetime.date(2025, 8, 31))

	def test_accepts_iso_strings(self):
		self.assertEqual(parse_date("2025-08-31", True)[0], datetime.date(2025, 8, 31))

	def test_day_first_setting_decides_an_ambiguous_string(self):
		self.assertEqual(parse_date("05-08-2025", True)[0], datetime.date(2025, 8, 5))
		self.assertEqual(parse_date("05-08-2025", False)[0], datetime.date(2025, 5, 8))

	def test_accepts_slashes_and_dots(self):
		self.assertEqual(parse_date("31/08/2025", True)[0], datetime.date(2025, 8, 31))
		self.assertEqual(parse_date("31.08.2025", True)[0], datetime.date(2025, 8, 31))

	def test_rejects_dates_outside_1990_to_2100(self):
		self.assertIsNone(parse_date("31-08-1989", True)[0])
		self.assertIsNone(parse_date("31-08-2101", True)[0])
		self.assertIn("outside", parse_date("31-08-1989", True)[1])

	def test_rejects_nonsense(self):
		self.assertIsNone(parse_date("not a date", True)[0])
		self.assertIsNone(parse_date("31-13-2025", True)[0])
		self.assertIsNone(parse_date("", True)[0])


class TestAmounts(unittest.TestCase):
	def test_accepts_indian_and_western_grouping(self):
		self.assertEqual(parse_amount("1,46,250.00")[0], parse_amount("146,250.00")[0])
		self.assertEqual(float(parse_amount("1,46,250.00")[0]), 146250.00)

	def test_strips_currency_decoration(self):
		for text in ("₹ 1,46,250.00", "Rs. 1,46,250.00", "Rs 146250", "INR 146250.00"):
			self.assertEqual(float(parse_amount(text)[0]), 146250.00, text)

	def test_survives_a_pdf_line_wrap(self):
		self.assertEqual(float(parse_amount("1,46,\n250.00")[0]), 146250.00)

	def test_blank_is_zero(self):
		self.assertEqual(float(parse_amount(None)[0]), 0.0)
		self.assertEqual(float(parse_amount("   ")[0]), 0.0)

	def test_negatives_are_errors(self):
		self.assertIsNone(parse_amount("-100")[0])
		self.assertIsNone(parse_amount("(1,000.00)")[0])
		self.assertIn("negative", parse_amount(-100)[1])

	def test_rubbish_is_an_error(self):
		self.assertIsNone(parse_amount("abc")[0])
		self.assertIsNone(parse_amount("-")[0])

	def test_rounds_to_two_places_commercially(self):
		self.assertEqual(float(parse_amount("143203.125")[0]), 143203.13)


class TestStatus(unittest.TestCase):
	def test_blank_means_unpaid(self):
		self.assertEqual(parse_status(None)[0], "Unpaid")
		self.assertEqual(parse_status("  ")[0], "Unpaid")

	def test_case_insensitive(self):
		for text in ("Paid", "PAID", "paid"):
			self.assertEqual(parse_status(text)[0], "Paid")
		for text in ("Unpaid", "UNPAID", "unpaid"):
			self.assertEqual(parse_status(text)[0], "Unpaid")

	def test_anything_else_is_an_error(self):
		self.assertIsNone(parse_status("Partially Paid")[0])
		self.assertIn("not a valid status", parse_status("Settled")[1])


class TestSkippedRows(unittest.TestCase):
	def test_blank_rows_are_skipped(self):
		rows = [
			HEADER,
			["31-08-2025", "0", "146250", "", ""],
			[],
			["", "", "", "", ""],
			["30-09-2025", "0", "146250", "", ""],
		]
		parsed, errors = parse(rows)
		self.assertEqual(errors, [])
		self.assertEqual(len(parsed), 2)

	def test_a_spacer_row_claiming_no_payment_is_skipped(self):
		rows = [HEADER, ["31-08-2025", "0", "146250", "", ""], ["", "", "", "", ""]]
		parsed, errors = parse(rows)
		self.assertEqual(errors, [])
		self.assertEqual(len(parsed), 1)

	def test_but_a_row_claiming_a_payment_is_not_skipped(self):
		rows = [HEADER, ["", "", "", "Paid", ""]]
		_parsed, errors = parse(rows)
		self.assertTrue(any("date is missing" in e for e in errors), errors)


class TestRowRules(unittest.TestCase):
	def test_a_reference_on_an_unpaid_row_is_an_error(self):
		rows = [HEADER, ["31-08-2025", "0", "146250", "Unpaid", "ACC-JV-2026-00001"]]
		_parsed, errors = parse(rows)
		self.assertTrue(any("status is Unpaid" in e for e in errors), errors)

	def test_a_paid_row_cannot_fall_due_in_the_future(self):
		rows = [HEADER, ["30-09-2026", "0", "146250", "Paid", ""]]
		_parsed, errors = parse(rows)
		self.assertTrue(any("in the future" in e for e in errors), errors)

	def test_due_dates_must_strictly_increase(self):
		rows = [HEADER, ["30-09-2025", "0", "1", "", ""], ["31-08-2025", "0", "1", "", ""]]
		_parsed, errors = parse(rows)
		self.assertTrue(any("does not come after" in e for e in errors), errors)

	def test_a_repeated_due_date_is_an_error(self):
		rows = [HEADER, ["31-08-2025", "0", "1", "", ""], ["31-08-2025", "0", "1", "", ""]]
		_parsed, errors = parse(rows)
		self.assertTrue(any("does not come after" in e for e in errors), errors)

	def test_a_due_date_before_disbursement_is_an_error(self):
		rows = [HEADER, ["31-07-2025", "0", "1", "", ""]]
		_parsed, errors = parse(rows, disbursement_date=datetime.date(2025, 8, 5))
		self.assertTrue(any("before the facility was disbursed" in e for e in errors), errors)

	def test_errors_name_the_sheet_row(self):
		rows = [["Title"], HEADER, ["31-08-2025", "0", "1", "", ""], ["bad", "0", "1", "", ""]]
		_parsed, errors = parse(rows)
		# title row 1, header row 2, first instalment row 3, bad row 4
		self.assertTrue(any(e.startswith("Row 4:") for e in errors), errors)

	def test_every_problem_is_collected_not_just_the_first(self):
		rows = [
			HEADER,
			["bad date", "0", "1", "", ""],
			["31-08-2025", "-5", "1", "", ""],
			["30-09-2025", "0", "1", "Settled", ""],
		]
		_parsed, errors = parse(rows)
		self.assertGreaterEqual(len(errors), 3, errors)


class TestWholeFile(unittest.TestCase):
	def test_reads_a_realistic_annexure(self):
		rows = [
			["ICICI Bank Limited", "", "", "", ""],
			["Annexure I", "", "", "", ""],
			[],
			["Sr", "Due Date", "Principal (in Rupees)", "Interest (in Rupees)", "Status"],
			[1, "31-08-2025", "", "₹ 1,46,250.00", "Paid"],
			[2, "30-09-2025", "0.00", "1,46,250.00", "Paid"],
			[3, "31-08-2026", "4,06,250.00", "1,46,250.00", ""],
		]
		parsed, errors = parse(rows, disbursement_date=datetime.date(2025, 8, 5))
		self.assertEqual(errors, [], errors)
		self.assertEqual(len(parsed), 3)
		self.assertEqual(parsed[0]["interest_amount"], 146250.00)
		self.assertEqual(parsed[0]["principal_amount"], 0.0)
		self.assertEqual(parsed[0]["payment_status"], "Paid")
		self.assertEqual(parsed[2]["principal_amount"], 406250.00)
		self.assertEqual(parsed[2]["payment_status"], "Unpaid")

	def test_a_totals_row_is_reported_rather_than_silently_dropped(self):
		"""A row with amounts but no date is ambiguous, so it is not skipped.

		Bank annexures end with a totals line. It carries principal and
		interest but no due date, so the skip rule -- which needs all three
		blank -- does not apply to it. Reporting it is the safer reading: the
		same shape is produced by a real instalment whose date failed to parse,
		and dropping that silently would lose money from the schedule.
		"""
		rows = [
			HEADER,
			["31-08-2025", "0", "1,46,250.00", "", ""],
			[],
			["Total", "4,06,250.00", "4,38,750.00", "", ""],
		]
		parsed, errors = parse(rows)
		self.assertEqual(len(parsed), 1)
		self.assertEqual(len(errors), 1, errors)
		self.assertTrue(errors[0].startswith("Row 4:"), errors)
		self.assertIn("not a valid date", errors[0])

	def test_a_header_with_no_instalments_is_reported(self):
		parsed, errors = parse([HEADER])
		self.assertEqual(parsed, [])
		self.assertIn("no instalments", errors[0])


if __name__ == "__main__":
	unittest.main(verbosity=2)
