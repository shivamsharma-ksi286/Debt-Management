# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

"""Where the group's term debt stands, one row per facility.

Revolving facilities are deliberately absent. A Cash Credit or Overdraft
carries no schedule and no outstanding figure on its own document -- its
balance lives in the general ledger -- so every money column here would read
zero for one and suggest the facility had been repaid.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

# Filters that map straight onto a Debt Facility field.
DIRECT_FILTERS = ("company", "loan_provider", "provider_type", "loan_type", "status")

FIELDS = (
	"name",
	"loan_provider",
	"company",
	"status",
	"total_payable",
	"total_interest_payable",
	"outstanding_principal",
	"total_principal_paid",
	"total_interest_paid",
	"next_due_date",
	"next_due_amount",
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_columns():
	"""Column definitions, in the order the report reads left to right.

	The currency column is hidden and carries each row's own company currency,
	which every money column points at through its ``options``. Without it a
	four-company group would have its columns rendered in one currency, and a
	total row that silently added rupees to something else.
	"""
	return [
		{
			"label": _("Loan ID"),
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "Debt Facility",
			"width": 150,
		},
		{
			"label": _("Loan Provider"),
			"fieldname": "loan_provider",
			"fieldtype": "Link",
			"options": "Loan Provider",
			"width": 180,
		},
		{
			"label": _("Company"),
			"fieldname": "company",
			"fieldtype": "Link",
			"options": "Company",
			"width": 160,
		},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{
			"label": _("Currency"),
			"fieldname": "currency",
			"fieldtype": "Link",
			"options": "Currency",
			"hidden": 1,
			"width": 100,
		},
		{
			"label": _("Total Payable"),
			"fieldname": "total_payable",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 140,
		},
		{
			"label": _("Total Interest Payable"),
			"fieldname": "total_interest_payable",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 160,
		},
		{
			"label": _("Outstanding Principal"),
			"fieldname": "outstanding_principal",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 160,
		},
		{
			"label": _("Total Principal Paid"),
			"fieldname": "total_principal_paid",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 150,
		},
		{
			"label": _("Total Interest Paid"),
			"fieldname": "total_interest_paid",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 150,
		},
		{
			"label": _("Next Due Date"),
			"fieldname": "next_due_date",
			"fieldtype": "Date",
			"width": 120,
		},
		{
			"label": _("Next Due Amount"),
			"fieldname": "next_due_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 140,
		},
	]


def get_data(filters):
	"""Submitted term facilities, sorted by what falls due soonest.

	Read through frappe.get_list rather than a raw query, so the user's own
	permissions and any user permission on Company apply. A report is the
	easiest place to leak another company's debt position.
	"""
	rows = frappe.get_list(
		"Debt Facility",
		filters=get_conditions(filters),
		fields=list(FIELDS),
		limit_page_length=0,
	)

	currencies = get_company_currencies({row.company for row in rows})

	for row in rows:
		row["currency"] = currencies.get(row.company)

	return sorted(rows, key=sort_key)


def get_conditions(filters):
	"""Turn the filter panel into frappe.get_list filters.

	Returned as a list of triples rather than a dict because two conditions are
	needed on next_due_date, which a dict cannot express.

	Only submitted, non-revolving facilities are ever considered; those two are
	not filters the user can relax.
	"""
	conditions = [["docstatus", "=", 1], ["is_revolving", "=", 0]]

	for fieldname in DIRECT_FILTERS:
		if filters.get(fieldname):
			conditions.append([fieldname, "=", filters.get(fieldname)])

	if filters.get("in_scope"):
		conditions.append(["in_scope", "=", 1])

	if filters.get("next_due_up_to"):
		# The "is set" test is load-bearing. Frappe compiles a comparison
		# filter to IFNULL(`next_due_date`, '') <= ..., so a facility with no
		# due date compares as an empty string and would pass -- the opposite
		# of what a "due up to" question means. Nothing is due on a facility
		# with no due date, so it is excluded explicitly.
		conditions.append(["next_due_date", "is", "set"])
		conditions.append(["next_due_date", "<=", getdate(filters.get("next_due_up_to"))])

	return conditions


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


def sort_key(row):
	"""Soonest due first, undated facilities last, then by Loan ID.

	A facility with no next due date is not overdue and not urgent -- it is
	usually fully repaid or newly submitted -- so it belongs at the bottom
	rather than sorting to the top as an empty value would.
	"""
	return (row.next_due_date is None, row.next_due_date or getdate("2100-01-01"), row.name)


def get_total_outstanding(filters=None):
	"""Outstanding across the filtered facilities, for callers wanting one figure."""
	return flt(sum(flt(row.outstanding_principal) for row in get_data(frappe._dict(filters or {}))))
