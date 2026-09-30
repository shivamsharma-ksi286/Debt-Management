app_name = "panorama_debt"
app_title = "Panorama Debt"
app_publisher = "Ksolves India Limited"
app_description = "Debt and borrowings management for the Panorama group of companies"
app_email = "sales@ksolves.com"
app_license = "mit"

# Apps
# ------------------
# ERPNext supplies Company, Account and Supplier, which every DocType in this
# app links to. Frappe is implicit.
required_apps = ["erpnext"]

# Shown as a tile on the /apps desk screen, routed at the Debt Management
# workspace. The slug is the workspace label scrubbed by frappe, so renaming
# the workspace means changing this route too.
add_to_apps_screen = [
	{
		"name": "panorama_debt",
		"logo": "/assets/panorama_debt/images/logo.svg",
		"title": "Panorama Debt",
		"route": "/desk/debt-management",
		"has_permission": "panorama_debt.api.permission.has_app_permission",
	}
]

# Includes in <head>
# ------------------
# app_include_css = "/assets/panorama_debt/css/panorama_debt.css"
# app_include_js = "/assets/panorama_debt/js/panorama_debt.js"

# DocType Class
# ---------------
# override_doctype_class = {}

# Document Events
# ---------------
# doc_events = {}

# Scheduled Tasks
# ---------------
# Build step 8 registers the daily overdue / next-due refresh job here.
# scheduler_events = {
# 	"daily": [
# 		"panorama_debt.debt_management.tasks.daily.update_overdue_instalments",
# 	],
# }

# Testing
# -------
# before_tests = "panorama_debt.install.before_tests"

# Fixtures
# --------
# Build step 8 exports the Debt Manager / Debt User / Management roles here.
# The dashboard block ships with the app: it is app data, not site data, and a
# fresh install should show the dues table without anyone rebuilding it. The
# roles step extends this list.
fixtures = [
	{"dt": "Custom HTML Block", "filters": [["name", "=", "Upcoming Loan Dues"]]},
]
