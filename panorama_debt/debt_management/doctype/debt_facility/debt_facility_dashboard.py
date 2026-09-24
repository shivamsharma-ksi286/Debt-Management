# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

from frappe import _


def get_data():
	"""Connections shown on a Debt Facility, linked through the schedule's `loan` field."""
	return {
		"fieldname": "loan",
		"transactions": [
			{
				"label": _("Repayment"),
				"items": ["Debt Repayment Schedule"],
			},
		],
	}
