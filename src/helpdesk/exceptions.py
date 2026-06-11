class IgnoreTicketException(Exception):
    """
    Raised when an email message is received from a sender who is marked to be ignored
    """

    pass


class DeleteIgnoredTicketException(Exception):
    """
    Raised when an email message is received from a sender who is marked to be ignored
    and the record is tagged to delete the email from the inbox
    """

    pass


class BypassTicketException(Exception):
    """
    Raised when an email should be bypassed (kept in mailbox, no ticket created).

    Two scenarios trigger this:
      1. A routing rule matched explicitly with `bypass=True`.
      2. No routing rule matched the email (default fallback for unrouted mail).
    """

    def __init__(self, reason: str = "No matching routing rule", rule=None):
        self.reason = reason
        self.rule = rule
        super().__init__(reason)
