"""HTTP retry behavior shared by govbooks and govweb."""

from govbooks.http import CappedRetry, Http
from govweb.fetch import Fetcher


class Response:
    def __init__(self, retry_after):
        self.headers = {"Retry-After": retry_after}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


def test_retry_after_is_capped():
    retry = CappedRetry(total=2, respect_retry_after_header=True)
    assert retry.get_retry_after(Response("3600")) == CappedRetry.MAX_WAIT  # a used-up daily quota
    assert retry.get_retry_after(Response("5")) == 5
    assert type(retry.new(total=1)) is CappedRetry  # the cap survives each retry


def test_quota_refusals_are_not_retried_by_the_api_client():
    retry = Http().session.get_adapter("https://").max_retries
    assert isinstance(retry, CappedRetry)
    assert 429 not in retry.status_forcelist and 503 in retry.status_forcelist


def test_crawler_fails_fast_on_connect_errors():
    retry = Fetcher().session.get_adapter("https://").max_retries
    assert isinstance(retry, CappedRetry) and retry.connect == 0
