"""PMC's metadata and its licence codes (ADR-0036, #458): only CC licences are admitted."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_trainer.domain.knowledge import LicenseId, content_hash
from ai_trainer.domain.pmc import PmcArticle, pmc_attribution, pmc_licence, pmc_placeholder_hash

FIXTURE = Path(__file__).parents[1] / "fixtures" / "pmc" / "PMC13692514.1.json"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("CC0", LicenseId.CC0),
        ("CC BY", LicenseId.CC_BY),
        ("CC BY-SA", LicenseId.CC_BY_SA),
        ("CC BY-NC", LicenseId.CC_BY_NC),
        ("CC BY-NC-SA", LicenseId.CC_BY_NC_SA),
        ("CC BY-ND", LicenseId.CC_BY_ND),
        ("CC BY-NC-ND", LicenseId.CC_BY_NC_ND),
        # Spelling drift: case, extra spaces, a hyphen after CC.
        (" cc  by-nc ", LicenseId.CC_BY_NC),
        ("CC-BY", LicenseId.CC_BY),
    ],
)
def test_only_cc_codes_are_admitted(code: str, expected: LicenseId) -> None:
    assert pmc_licence(code) is expected


@pytest.mark.parametrize("code", ["TDM", "implied-oa", None, "", "free to read", "CC BY-XYZ"])
def test_tdm_missing_and_other_codes_are_refused(code: str | None) -> None:
    assert pmc_licence(code) is None


def test_the_bucket_json_reads_as_an_article() -> None:
    article = PmcArticle.model_validate_json(FIXTURE.read_bytes())

    assert article.pmcid == "PMC13692514"
    assert article.version == 1
    assert article.pmid == "42924713"
    assert article.doi == "10.3390/jfmk11040434"
    assert article.title.startswith("Resistance Training Improves Respiratory Muscle Strength")
    assert article.license_code == "CC BY"
    assert article.is_retracted is False


@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        ("10.1000/ABC", "10.1000/abc"),
        ("https://doi.org/10.1000/x", "10.1000/x"),
        ("not a doi", None),
        ("", None),
        (None, None),
        (12345, None),
        ({"value": "10.1000/x"}, None),
    ],
)
def test_a_doi_is_kept_only_when_it_is_one(doi: object, expected: str | None) -> None:
    article = PmcArticle.model_validate(
        {"pmcid": "PMC1", "version": 1, "title": "T", "is_retracted": False, "doi": doi}
    )
    assert article.doi == expected


@pytest.mark.parametrize(
    ("pmid", "expected"),
    [(42924713, "42924713"), ("42924713", "42924713"), ("PMID 1", None), ("", None),
     (True, None), (None, None)],
)  # fmt: skip
def test_a_pmid_is_kept_only_when_it_is_one(pmid: object, expected: str | None) -> None:
    # A bad PMID costs the source its PMID, never the source (the template would refuse it).
    article = PmcArticle.model_validate(
        {"pmcid": "PMC1", "version": 1, "title": "T", "is_retracted": False, "pmid": pmid}
    )
    assert article.pmid == expected


def test_metadata_without_a_title_is_malformed() -> None:
    with pytest.raises(ValidationError):
        PmcArticle.model_validate({"pmcid": "PMC1", "version": 1, "title": " ",
                                   "is_retracted": False})  # fmt: skip


def test_the_attribution_names_the_work_its_licence_and_nlm() -> None:
    article = PmcArticle(
        pmcid="PMC1", version=1, title="A trial.", citation="J X. 2026;1:2.",
        is_retracted=False, license_code="CC BY",
    )  # fmt: skip
    assert pmc_attribution(article) == "A trial. J X. 2026;1:2. Licence: CC BY. Source: NLM PMC."
    bare = article.model_copy(update={"citation": None})
    assert pmc_attribution(bare) == "A trial. Licence: CC BY. Source: NLM PMC."


def test_the_placeholder_hash_is_unique_per_article_and_never_a_files() -> None:
    assert pmc_placeholder_hash("PMC1") == content_hash(b"pmc:PMC1")
    assert pmc_placeholder_hash("PMC1") != pmc_placeholder_hash("PMC2")
