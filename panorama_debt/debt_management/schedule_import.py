# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

"""Parsing for repayment schedules that arrive as a spreadsheet.

Banks send their annexure as an Excel file, a CSV, or a table copied out of a
PDF, and no two of them agree on headings, column order, date format or how a
number is punctuated. This module turns any of that into rows the schedule can
use, and reports everything wrong with the file at once rather than stopping at
the first bad cell -- a user fixing an upload wants the whole list, not twenty
round trips.

Pure functions on primitives: nothing here reads or writes a document, so the
whole module is unit-testable without a site.
"""

import datetime
import re
from decimal import Decimal, InvalidOperation

from frappe import _

from panorama_debt.debt_management.schedule_engine import ZERO, money

# How far into the file we will look for the heading row. Bank annexures carry
# a title, an address block and a sanction reference above the table.
MAX_HEADER_SCAN_ROWS = 10

# Dates outside this range are a misread serial number or a typo, not a real
# instalment: no facility in this app amortises outside it.
MIN_YEAR = 1990
MAX_YEAR = 2100

# Excel counts days from 1899-12-30 (its epoch, including the 1900 leap-year
# bug that Lotus 1-2-3 introduced and Excel kept).
EXCEL_EPOCH = datetime.date(1899, 12, 30)

# Headings seen in the wild, normalised. Matching is by exact normalised text
# first, then by prefix, so "principal" also catches "principal repayment".
COLUMN_ALIASES = {
	"due_date": (
		"due date",
		"date",
		"instalment date",
		"installment date",
		"emi date",
		"repayment date",
		"payment date",
		"due",
	),
	"principal_amount": (
		"principal amount",
		"principal",
		"principal repayment",
		"principal component",
		"principal due",
	),
	"interest_amount": ("interest amount", "interest", "interest component", "interest due"),
	"payment_status": ("status", "payment status", "paid status"),
	"payment_entry": (
		"payment entry reference",
		"payment entry",
		"journal entry",
		"reference",
		"reference no",
		"voucher",
	),
}

# Columns the heading row must offer before we believe it is the heading row.
REQUIRED_COLUMNS = ("due_date", "principal_amount", "interest_amount")

VALID_STATUSES = {"paid": "Paid", "unpaid": "Unpaid", "": "Unpaid"}

# Currency decoration to strip before a number is read.
CURRENCY_NOISE = re.compile(r"(?:₹|rs\.?|inr)", re.IGNORECASE)
NON_NUMERIC = re.compile(r"[^0-9.\-]")


def normalise_heading(value) -> str:
	"""Reduce a heading to comparable text.

	Case, surrounding whitespace, line breaks and a parenthesised qualifier are
	all noise: "Principal (in Rupees)" and "principal" are the same column. The
	parenthesis rule is what lets bank templates through without an alias each.
	"""
	if value is None:
		return ""

	text = str(value).replace("\n", " ").strip().lower()
	text = re.sub(r"\(.*?\)", " ", text)
	text = re.sub(r"[^a-z0-9 ]", " ", text)
	return re.sub(r"\s+", " ", text).strip()


def match_column(heading: str):
	"""Which field a heading names, or None if it names nothing we want."""
	if not heading:
		return None

	for fieldname, aliases in COLUMN_ALIASES.items():
		if heading in aliases:
			return fieldname

	for fieldname, aliases in COLUMN_ALIASES.items():
		if any(heading.startswith(alias) for alias in aliases):
			return fieldname

	return None


def find_header(raw_rows):
	"""Locate the heading row and map each wanted field to its column index.

	Returns ``(index, columns)`` or ``(None, {})``. Only the first
	MAX_HEADER_SCAN_ROWS non-blank rows are considered, and a row only counts
	if it offers every required column -- otherwise a stray line such as
	"Sanction Date: 01-08-2025" could be mistaken for the table heading.
	"""
	scanned = 0

	for index, row in enumerate(raw_rows):
		if is_blank_row(row):
			continue

		scanned += 1
		if scanned > MAX_HEADER_SCAN_ROWS:
			break

		columns = {}
		for position, cell in enumerate(row):
			fieldname = match_column(normalise_heading(cell))
			# First occurrence wins, so a trailing duplicate column is ignored.
			if fieldname and fieldname not in columns:
				columns[fieldname] = position

		if all(fieldname in columns for fieldname in REQUIRED_COLUMNS):
			return index, columns

	return None, {}


def is_blank_row(row) -> bool:
	return not any(str(cell).strip() for cell in row if cell is not None)


def cell(row, columns, fieldname):
	"""One cell by field name, or None when the file has no such column."""
	position = columns.get(fieldname)
	if position is None or position >= len(row):
		return None
	return row[position]


def parse_date(value, dayfirst):
	"""Read a due date from whatever the spreadsheet handed us.

	Four shapes turn up: a real date cell, an Excel serial number, an ISO
	string, and a dd-mm-yyyy string. The last two are ambiguous for the first
	twelve days of a month, which is what ``dayfirst`` settles -- it follows
	the site's own date format so a file exported from this site reads back the
	way it was written.
	"""
	if value is None or (isinstance(value, str) and not value.strip()):
		return None, _("date is missing")

	if isinstance(value, datetime.datetime):
		return in_range(value.date())

	if isinstance(value, datetime.date):
		return in_range(value)

	if isinstance(value, int | float) and not isinstance(value, bool):
		try:
			return in_range(EXCEL_EPOCH + datetime.timedelta(days=int(value)))
		except OverflowError, ValueError:
			return None, _("{0} is not a valid date").format(value)

	text = str(value).strip().replace(".", "-").replace("/", "-")
	parts = text.split("-")

	if len(parts) == 3:
		try:
			numbers = [int(part) for part in parts]
		except ValueError:
			numbers = None

		if numbers:
			if len(parts[0]) == 4:
				year, first, second = numbers
				day, month = second, first
			else:
				year = numbers[2]
				day, month = (numbers[0], numbers[1]) if dayfirst else (numbers[1], numbers[0])

			try:
				return in_range(datetime.date(year, month, day))
			except ValueError:
				return None, _("{0} is not a valid date").format(value)

	return None, _("{0} is not a valid date").format(value)


def in_range(parsed):
	"""Keep a parsed date inside the years this app will amortise across."""
	if MIN_YEAR <= parsed.year <= MAX_YEAR:
		return parsed, None

	return None, _("{0} is outside {1}-{2}").format(parsed.isoformat(), MIN_YEAR, MAX_YEAR)


def parse_amount(value):
	"""Read a money figure, tolerating how spreadsheets and PDFs punctuate one.

	Indian and Western grouping both come out the same once commas go, a
	currency symbol is decoration, and a figure copied from a PDF may have a
	line break inside it. Accounting parentheses mean a negative, which is
	reported rather than silently flipped -- a schedule has no negative
	instalments, so it means the wrong column was mapped.
	"""
	if value is None or (isinstance(value, str) and not value.strip()):
		return ZERO, None

	if isinstance(value, bool):
		return None, _("{0} is not an amount").format(value)

	if isinstance(value, int | float | Decimal):
		amount = money(value)
		return (amount, None) if amount >= ZERO else (None, _("{0} is negative").format(value))

	text = str(value).strip()
	negative = text.startswith("(") and text.endswith(")")
	text = CURRENCY_NOISE.sub("", text)
	text = NON_NUMERIC.sub("", text.replace(",", ""))

	if not text or text in ("-", "."):
		return None, _("{0} is not an amount").format(value)

	try:
		amount = money(Decimal(text))
	except InvalidOperation, ValueError:
		return None, _("{0} is not an amount").format(value)

	if negative or amount < ZERO:
		return None, _("{0} is negative").format(value)

	return amount, None


def parse_status(value):
	"""Read the payment status. Blank means the instalment is still outstanding."""
	text = "" if value is None else str(value).strip().lower()

	if text in VALID_STATUSES:
		return VALID_STATUSES[text], None

	return None, _("{0} is not a valid status; use Paid or Unpaid").format(value)


def parse_reference(value):
	return str(value).strip() if value is not None and str(value).strip() else None


def parse_schedule_rows(raw_rows, *, dayfirst, today, disbursement_date=None):
	"""Turn spreadsheet rows into schedule rows, collecting every problem found.

	Returns ``(rows, errors)``. Errors name the sheet row they came from, using
	the numbering the user sees in their spreadsheet, so a long list can be
	worked through top to bottom.

	Rows that are blank, or that carry no date, no principal and no interest
	and claim no payment, are skipped rather than reported: bank annexures are
	full of spacer lines and totals.
	"""
	errors = []
	header_index, columns = find_header(raw_rows)

	if header_index is None:
		return [], [
			_("No heading row found in the first {0} rows. The file needs columns for {1}.").format(
				MAX_HEADER_SCAN_ROWS, "Due Date, Principal Amount, Interest"
			)
		]

	rows = []
	previous_due = None

	for offset, raw in enumerate(raw_rows[header_index + 1 :], start=header_index + 2):
		if is_blank_row(raw):
			continue

		raw_date = cell(raw, columns, "due_date")
		raw_principal = cell(raw, columns, "principal_amount")
		raw_interest = cell(raw, columns, "interest_amount")
		status_value = cell(raw, columns, "payment_status")
		reference = parse_reference(cell(raw, columns, "payment_entry"))

		claims_payment = bool(reference) or bool(str(status_value or "").strip())
		if not claims_payment and all(
			value is None or not str(value).strip() for value in (raw_date, raw_principal, raw_interest)
		):
			continue

		def fail(message, offset=offset):
			errors.append(_("Row {0}: {1}").format(offset, message))

		due_date, error = parse_date(raw_date, dayfirst)
		if error:
			fail(error)

		principal, error = parse_amount(raw_principal)
		if error:
			fail(_("principal {0}").format(error))

		interest, error = parse_amount(raw_interest)
		if error:
			fail(_("interest {0}").format(error))

		status, error = parse_status(status_value)
		if error:
			fail(error)

		if reference and status == "Unpaid":
			fail(_("a payment reference was given but the status is Unpaid"))

		if due_date:
			if status == "Paid" and due_date > today:
				fail(
					_("marked Paid but falls due on {0}, which is in the future").format(due_date.isoformat())
				)

			if disbursement_date and due_date < disbursement_date:
				fail(
					_("falls due on {0}, before the facility was disbursed on {1}").format(
						due_date.isoformat(), disbursement_date.isoformat()
					)
				)

			if previous_due and due_date <= previous_due:
				fail(
					_("due date {0} does not come after the previous row's {1}").format(
						due_date.isoformat(), previous_due.isoformat()
					)
				)
			else:
				previous_due = due_date

		if due_date and principal is not None and interest is not None and status:
			rows.append(
				{
					"due_date": due_date,
					"principal_amount": float(principal),
					"interest_amount": float(interest),
					"payment_status": status,
					"payment_entry": reference,
				}
			)

	if not rows and not errors:
		errors.append(_("The file has a heading row but no instalments under it."))

	return rows, errors
