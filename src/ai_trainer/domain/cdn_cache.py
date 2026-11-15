"""The CDN's cached copies (ADR-0031 `removed`, #557)."""


class CdnCacheError(Exception):
    """A purge did not go through. The message names the status or the exception class only:
    never a header, the API call's URL or a response body, which could carry the token."""
