"""Shared data model + constants for the approval-flow project.

Amounts are stored as integer paise (never floats) so a retry can never
turn Rs 18,450.00 into Rs 18,449.99.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TASK_QUEUE = "approval-flow"
TEMPORAL_ADDRESS = "localhost:7233"
MOCK_SLACK_URL = "http://127.0.0.1:18765"

# Vendors we have a buying relationship with. Anything else fails validation
# and never reaches a human approver.
KNOWN_VENDORS = {
    "Varagi Trades Hyper",
    "Savitha Mart",
    "All Food Supplies",
    "JioMart",
    "Instamart",
    "Blinkit",
}

# Invoices above this need a second approver in the real world; here they
# simply fail validation so the workflow never silently pays a huge bill.
MAX_AUTO_AMOUNT_PAISE = 100_000_00  # Rs 1,00,000

# How long the workflow waits for a human decision before timing out.
APPROVAL_TIMEOUT_SECONDS = 600


def paise_to_rupees(paise: int) -> str:
    """1845000 -> 'Rs 18,450.00' (Indian digit grouping)."""
    rupees, p = divmod(int(paise), 100)
    return f"Rs {rupees:,}." + f"{p:02d}"


@dataclass
class Invoice:
    invoice_id: str
    vendor: str | None = None
    amount_paise: int | None = None
    currency: str = "INR"
    invoice_date: str | None = None
    due_date: str | None = None
    description: str | None = None
    status: str = "SEEDED"
    reason: str | None = None
    extracted: dict = field(default_factory=dict)

    @property
    def workflow_id(self) -> str:
        return f"invoice-{self.invoice_id}"

    @property
    def idempotency_key(self) -> str:
        """Deterministic payment key: same invoice + same amount = same key,
        so a retry can never double-pay. Deliberately NOT random."""
        return f"pay:{self.invoice_id}:{self.amount_paise}"
