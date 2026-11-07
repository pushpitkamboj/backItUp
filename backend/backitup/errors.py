import re


class BackItUpError(Exception):
    def __init__(self, code: str, message: str, *, transient: bool = False, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.transient = transient
        self.status = status


def redact(value: str) -> str:
    value = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[redacted]@", value)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[redacted]", value)
    value = re.sub(r"(?i)(authorization|access_token|refresh_token|secret_access_key|password)[\s\"':=]+[^\s,}\"]+", r"\1=[redacted]", value)
    return value[:2000]
