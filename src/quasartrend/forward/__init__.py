"""Forward-capture boundaries.  These modules are deliberately non-executing."""

from .xm import (
    DemoOnlyExecutionGuard, GrossNetAccounting, SignalJournal, TickJournal,
    config_hash, separate_gross_and_net,
)
from .mt5 import (
    CapabilityAudit, DemoExecutionAdapter, Family1LongOnlyShadow, JsonlJournal,
    XMForwardService, audit_capabilities, environment_pseudonym,
    holdout_aggregate_economics, load_mt5,
)

__all__ = ["CapabilityAudit", "DemoExecutionAdapter", "DemoOnlyExecutionGuard", "Family1LongOnlyShadow", "GrossNetAccounting", "JsonlJournal", "SignalJournal", "TickJournal", "XMForwardService", "audit_capabilities", "config_hash", "environment_pseudonym", "holdout_aggregate_economics", "load_mt5", "separate_gross_and_net"]
