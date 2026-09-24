# Copyright (c) 2026, Ksolves India Limited and contributors
# For license information, please see license.txt

import frappe


def has_app_permission() -> bool:
	"""Whether the current user should see Panorama Debt on the apps screen.

	Gated on read access to Loan Provider rather than on a hard-coded list of
	role names. Every role that has any business in this app can read the
	provider master, so the tile appears for Debt Manager, Debt User and
	Management once step 8 creates those roles, and disappears for everyone
	else, with no second list of roles to keep in step with the DocType
	permissions.
	"""
	return bool(frappe.has_permission("Loan Provider", "read"))
