"""Fictitious network participants used by the simulator.

All bank names, routing numbers, account numbers, people and companies here are
made up. Routing numbers are generated with a valid ABA check digit so they pass
validation, but they are not meant to identify any real institution.
"""
from .nacha import routing

OPERATOR_ROUTING = routing("59990001")
OPERATOR_NAME = "SIM ACH OPERATOR"

FIRST_EXAMPLE_BANK = routing("51234567")   # the ODFI you play with the client
SECOND_SAMPLE_BANK = routing("52000002")   # RDFI simulated by the operator
THIRD_DEMO_CU = routing("53000003")        # RDFI simulated by the operator; also originates

# mode "mailbox": files addressed to this bank wait in its mailbox for the client to pull.
# mode "auto":    the simulator plays this bank's RDFI and decides post/return/NOC itself
#                 (a copy of every file is still left in its mailbox for inspection).
PARTICIPANTS = {
    FIRST_EXAMPLE_BANK: {"name": "FIRST EXAMPLE BANK", "api_key": "feb-demo-key",
                         "mode": "mailbox"},
    SECOND_SAMPLE_BANK: {"name": "SECOND SAMPLE BANK", "api_key": "ssb-demo-key",
                         "mode": "auto"},
    THIRD_DEMO_CU: {"name": "THIRD DEMO CREDIT UNION", "api_key": "tdcu-demo-key",
                    "mode": "auto"},
}

# Receiver accounts held at the operator-simulated RDFIs. The RDFI decision engine
# (rdfi.py) uses these to produce realistic returns and NOCs.
#   status: open | closed | frozen
#   type:   checking | savings   (mismatch with the transaction code -> NOC C05)
#   balance: cents available for debits (debit > balance -> R01)
#   disputes: originator company ids the account holder says are unauthorized
#             (consumer SEC -> R10, CCD -> R29)
#   noc: (change code, corrected data) to send back after posting
RDFI_ACCOUNTS = {
    SECOND_SAMPLE_BANK: {
        "1000234567": {"holder": "JANE Q PUBLIC", "status": "open", "type": "checking",
                       "balance": 2_500_00},
        "1000777001": {"holder": "OMAR FIELDING", "status": "open", "type": "savings",
                       "balance": 9_000_00},  # payroll sent as checking -> C05
        "1000555123": {"holder": "LENA MARSH", "status": "closed", "type": "checking",
                       "balance": 0},          # -> R02
        "2000188800": {"holder": "EXAMPLE SUPPLY LLC", "status": "open",
                       "type": "checking", "balance": 50_000_00},
        "2000199900": {"holder": "SAMPLE DESIGN INC", "status": "open",
                       "type": "checking", "balance": 80_000_00,
                       "disputes": ["1987654321"]},  # CCD debit -> R29
        "3000400401": {"holder": "SAM RIVERA", "status": "open", "type": "checking",
                       "balance": 45_10},       # WEB debit 89.99 -> R01
        "3000400402": {"holder": "PRIYA NAIR", "status": "open", "type": "checking",
                       "balance": 1_200_00, "disputes": ["9876500001"]},  # -> R10
    },
    THIRD_DEMO_CU: {
        "55501234": {"holder": "DEVON PARK", "status": "open", "type": "checking",
                     "balance": 3_100_00, "noc": ("C01", "55501243")},  # transposed digits
        "55509876": {"holder": "AISHA BELL", "status": "open", "type": "savings",
                     "balance": 700_00},
        "55507777": {"holder": "MARCO DIAZ", "status": "open", "type": "checking",
                     "balance": 25_000_00},
        "55600001": {"holder": "DEMO FREIGHT CO", "status": "open", "type": "checking",
                     "balance": 120_000_00},
    },
}
