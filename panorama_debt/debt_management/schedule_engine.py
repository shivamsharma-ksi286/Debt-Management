"""Amortisation schedule engine for Panorama Debt.

Pure functions only. Every entry point takes primitives (numbers, strings,
dates) and returns plain Python data structures, so nothing here imports or
touches a DocType and the whole module is unit-testable on its own.

Money arithmetic runs on :class:`decimal.Decimal` quantised with ROUND_HALF_UP
-- commercial rounding, ties away from zero -- and is converted to float only
on the way out. A Frappe site may be configured for banker's rounding; bank
sanction letters never are, so the engine deliberately does its own rounding
rather than relying on ``flt``. Sanity check: 143203.125 rounds to 143203.13,
not 143203.12.

Interest is charged 30/360 style on the *opening* balance of each period,
which is what Indian banks print in sanction-letter annexures.
"""

from decimal import ROUND_HALF_UP, Decimal

from frappe.utils import add_months, getdate

# --- vocabulary -------------------------------------------------------------

PERIODS_PER_YEAR = {"Monthly": 12, "Quarterly": 4, "Half Yearly": 2, "Yearly": 1}
MONTHS_PER_PERIOD = {"Monthly": 1, "Quarterly": 3, "Half Yearly": 6, "Yearly": 12}

EMI = "Equal Instalment (EMI)"
EQUAL_PRINCIPAL = "Equal Principal"
INTEREST_ONLY_THEN_EMI = "Interest Only then EMI"
INTEREST_ONLY_THEN_EQUAL_PRINCIPAL = "Interest Only then Equal Principal"
BULLET = "Bullet"
MANUAL = "As per Bank Schedule (Manual)"

INTEREST_SERVICING = "Interest Servicing"
INTEREST_CAPITALISED = "Full Moratorium (Interest Capitalised)"

# The method each repayment_method amortises with once its moratorium, if any,
# is over. "Interest Only then X" is simply X with a leading interest-only run.
AMORTISING_METHOD = {
	EMI: EMI,
	INTEREST_ONLY_THEN_EMI: EMI,
	EQUAL_PRINCIPAL: EQUAL_PRINCIPAL,
	INTEREST_ONLY_THEN_EQUAL_PRINCIPAL: EQUAL_PRINCIPAL,
	BULLET: BULLET,
}

REPAYMENT_METHODS = (*AMORTISING_METHOD, MANUAL)

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


# --- primitives -------------------------------------------------------------


def _dec(value) -> Decimal:
	"""Coerce a number to Decimal without inheriting binary float error.

	Routing through ``str`` means 0.1 arrives as Decimal("0.1") rather than as
	the binary double that is fractionally above it, which keeps half-way
	values genuinely half-way so ROUND_HALF_UP can do its job.
	"""
	if isinstance(value, Decimal):
		return value
	if value is None or value == "":
		return ZERO
	return Decimal(str(value))


def money(value) -> Decimal:
	"""Round a value to 2 decimals, half away from zero (commercial rounding).

	This is the only rounding used anywhere in the engine. It is NOT Python's
	default banker's rounding: 143203.125 -> 143203.13 and -143203.125 ->
	-143203.13.
	"""
	return _dec(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def get_period_rate(rate_of_interest, repayment_frequency) -> Decimal:
	"""Interest rate for one instalment period, as an unrounded fraction.

	The annual percentage is divided by 100 and then by the number of periods
	in a year, so 9% monthly gives 0.0075. Left unrounded: it is a multiplier,
	not a money figure. A zero rate is legitimate -- the group holds genuinely
	interest-free inter-corporate loans -- and is passed through untouched.
	"""
	try:
		periods = PERIODS_PER_YEAR[repayment_frequency]
	except KeyError:
		raise ValueError(f"Unsupported repayment frequency: {repayment_frequency!r}") from None

	rate = _dec(rate_of_interest)
	if rate < ZERO:
		raise ValueError("rate_of_interest cannot be negative")

	return rate / Decimal(100) / Decimal(periods)


def get_moratorium_periods(moratorium_months, repayment_frequency) -> int:
	"""Whole instalment periods covered by a moratorium quoted in months.

	Sanction letters quote a moratorium in months while instalments may be
	quarterly, half yearly or yearly, so the months are converted to whole
	periods: a 12-month moratorium on a quarterly loan is 4 instalments. For
	monthly loans -- the overwhelming majority -- the two are identical. Any
	partial trailing period is dropped, since a bank bills whole instalments.
	"""
	months = int(moratorium_months or 0)
	if months <= 0:
		return 0

	try:
		step = MONTHS_PER_PERIOD[repayment_frequency]
	except KeyError:
		raise ValueError(f"Unsupported repayment frequency: {repayment_frequency!r}") from None

	return months // step


def compute_emi(principal, period_rate, number_of_instalments) -> Decimal:
	"""Level instalment for an annuity, rounded to 2 decimals.

	EMI = P*r*(1+r)^n / ((1+r)^n - 1)

	At r == 0 that expression is 0/0, so the EMI degenerates to the arithmetic
	P / n with no interest component -- the interest-free inter-corporate case.
	The figure is rounded once here rather than per period, so every instalment
	in the schedule carries the identical amount a bank would quote, and the
	final instalment absorbs whatever residual the rounding leaves behind.
	"""
	p = _dec(principal)
	r = _dec(period_rate)
	n = int(number_of_instalments)

	if n <= 0:
		raise ValueError("number_of_instalments must be greater than 0")

	if r == ZERO:
		return money(p / Decimal(n))

	growth = (Decimal(1) + r) ** n
	return money(p * r * growth / (growth - Decimal(1)))


def get_due_dates(repayment_start_date, number_of_instalments, repayment_frequency) -> list:
	"""Due date of every instalment, first one on the repayment start date.

	Each later date is computed by adding whole months **to the start date**
	rather than to the preceding due date. Anchoring this way preserves the day
	of the month across short months -- a 31 Jan start runs 31 Jan, 28 Feb,
	31 Mar -- where chaining would drag the whole schedule onto the 28th.
	Days that do not exist in a month clamp to the month end.

	With no start date supplied the schedule is still pure arithmetic, so a
	list of None is returned and the caller fills the dates in later.
	"""
	n = int(number_of_instalments)

	try:
		step = MONTHS_PER_PERIOD[repayment_frequency]
	except KeyError:
		raise ValueError(f"Unsupported repayment frequency: {repayment_frequency!r}") from None

	if not repayment_start_date:
		return [None] * n

	anchor = getdate(repayment_start_date)
	return [add_months(anchor, i * step) for i in range(n)]


# --- schedule ---------------------------------------------------------------


def _row(number, due_date, opening, principal_amount, interest_amount, instalment_amount, closing):
	"""Build one schedule row in Loan Repayment Schedule Detail's own shape.

	``outstanding_principal`` is the balance at the START of the period, which
	is the column the bank's annexure charges its interest on. Every Decimal is
	already quantised to 2 decimals by the caller, so float() here is a
	representation change, not a rounding step.
	"""
	return {
		"instalment_number": number,
		"due_date": due_date,
		"outstanding_principal": float(opening),
		"principal_amount": float(principal_amount),
		"interest_amount": float(interest_amount),
		"other_charges": 0.0,
		"instalment_amount": float(instalment_amount),
		"closing_principal": float(closing),
	}


def generate_schedule(
	principal,
	rate_of_interest,
	number_of_instalments,
	repayment_method,
	repayment_frequency="Monthly",
	repayment_start_date=None,
	moratorium_months=0,
	moratorium_type=None,
):
	"""Full amortisation schedule as a list of dicts, one per instalment.

	``number_of_instalments`` is the total including any moratorium
	instalments, matching how the sanction letter counts them.

	A moratorium occupies the leading periods and behaves per ``moratorium_type``:

	* Interest Servicing -- principal 0, interest billed on the opening
	  balance, instalment equal to that interest.
	* Full Moratorium (Interest Capitalised) -- principal 0, instalment 0, and
	  the accrued interest is added to the outstanding principal, so the
	  grossed-up balance is what the remaining periods amortise.

	The remaining periods then run the method's underlying amortisation: a
	level EMI, a constant principal with a declining instalment, or -- for
	Bullet -- interest alone until the whole principal falls due at the end.
	The final instalment absorbs any rounding residual so that the closing
	principal lands exactly on 0.00.

	"As per Bank Schedule (Manual)" generates nothing and returns an empty
	list; those rows are keyed in from the bank's own annexure.
	"""
	if repayment_method == MANUAL:
		return []

	if repayment_method not in AMORTISING_METHOD:
		raise ValueError(f"Unsupported repayment method: {repayment_method!r}")

	total_instalments = int(number_of_instalments or 0)
	if total_instalments <= 0:
		raise ValueError("number_of_instalments must be greater than 0")

	opening = money(principal)
	if opening <= ZERO:
		raise ValueError("principal must be greater than 0")

	rate = get_period_rate(rate_of_interest, repayment_frequency)
	moratorium = get_moratorium_periods(moratorium_months, repayment_frequency)

	if moratorium >= total_instalments:
		raise ValueError("moratorium covers every instalment, leaving nothing to repay the principal")

	due_dates = get_due_dates(repayment_start_date, total_instalments, repayment_frequency)
	capitalising = moratorium_type == INTEREST_CAPITALISED
	rows = []

	# --- moratorium phase: no principal moves -------------------------------
	for index in range(moratorium):
		interest = money(opening * rate)
		closing = opening + interest if capitalising else opening
		instalment = ZERO if capitalising else interest

		rows.append(_row(index + 1, due_dates[index], opening, ZERO, interest, instalment, closing))
		opening = closing

	# --- amortising phase ---------------------------------------------------
	remaining = total_instalments - moratorium
	method = AMORTISING_METHOD[repayment_method]

	if method == EMI:
		instalment_amount = compute_emi(opening, rate, remaining)
	elif method == EQUAL_PRINCIPAL:
		principal_per_instalment = money(opening / Decimal(remaining))
	else:
		principal_per_instalment = ZERO

	for index in range(remaining):
		is_final = index == remaining - 1
		interest = money(opening * rate)

		if is_final:
			# Absorb every rounding residual here so closing lands on 0.00.
			principal_amount = opening
		elif method == EMI:
			principal_amount = money(instalment_amount - interest)
		else:
			principal_amount = principal_per_instalment

		if principal_amount > opening:
			principal_amount = opening
		elif principal_amount < ZERO:
			principal_amount = ZERO

		closing = opening - principal_amount
		rows.append(
			_row(
				moratorium + index + 1,
				due_dates[moratorium + index],
				opening,
				principal_amount,
				interest,
				principal_amount + interest,
				closing,
			)
		)
		opening = closing

	return rows


def summarise_schedule(rows):
	"""Totals across a generated schedule, for the parent document's fields.

	Sums the already-rounded row figures rather than recomputing from the loan
	terms, so the totals always reconcile with the rows on screen to the paisa.
	"""
	total_principal = sum((money(row["principal_amount"]) for row in rows), ZERO)
	total_interest = sum((money(row["interest_amount"]) for row in rows), ZERO)
	total_charges = sum((money(row.get("other_charges")) for row in rows), ZERO)

	return {
		"total_principal": float(total_principal),
		"total_interest": float(total_interest),
		"total_payable": float(total_principal + total_interest + total_charges),
	}
