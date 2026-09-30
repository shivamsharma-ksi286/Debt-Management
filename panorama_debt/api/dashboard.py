# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

"""Data for the Debt Management workspace blocks."""

import frappe

# A dashboard block is a glance, not a report. The cap stops a crafted call
# from turning a workspace widget into an unbounded table scan.
MAX_LIMIT = 50
DEFAULT_LIMIT = 10


@frappe.whitelist()
def get_upcoming_dues(limit=DEFAULT_LIMIT):
	"""Facilities with a payment coming up, soonest first.

	Read through frappe.get_list so the caller's own permissions and any
	Company user permission apply -- this is whitelisted, so it answers to
	whoever is logged in, not to the app.

	Only live term debt counts: a draft is not owed yet, a revolving facility
	has no instalments, an out-of-scope facility is excluded from group totals
	by definition, and a facility with no due date has nothing coming up.
	"""
	try:
		limit = int(limit or DEFAULT_LIMIT)
	except TypeError, ValueError:
		limit = DEFAULT_LIMIT

	limit = max(1, min(limit, MAX_LIMIT))

	rows = frappe.get_list(
		"Debt Facility",
		filters=[
			["docstatus", "=", 1],
			["is_revolving", "=", 0],
			["status", "=", "Active"],
			["in_scope", "=", 1],
			# Load-bearing: frappe compiles a comparison filter through
			# IFNULL(col, ''), so a facility with no due date would otherwise
			# pass a date test and be listed as though something were due.
			["next_due_date", "is", "set"],
		],
		fields=["name", "loan_provider", "next_due_date", "next_due_amount", "company"],
		order_by="next_due_date asc, name asc",
		limit_page_length=limit,
	)

	currencies = get_company_currencies({row.company for row in rows})

	return [
		{
			"name": row.name,
			"loan_provider": row.loan_provider,
			"next_due_date": row.next_due_date,
			"next_due_amount": row.next_due_amount,
			"currency": currencies.get(row.company),
		}
		for row in rows
	]


def get_company_currencies(companies):
	"""Default currency of every company in the result, in one query."""
	if not companies:
		return {}

	return dict(
		frappe.get_all(
			"Company",
			filters={"name": ("in", list(companies))},
			fields=["name", "default_currency"],
			as_list=True,
		)
	)
