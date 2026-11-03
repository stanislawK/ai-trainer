"""The one source template and its structured licence (ADR-0036, #456)."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.knowledge import (
    EvidenceTier,
    LicenceFlag,
    LicenseId,
    LicenseSource,
    SourceLanguage,
    SourceType,
    licence_flag,
)
from ai_trainer.domain.source_template import LicenceTerms, SourceTemplate

SPORTS = ("climbing", "gym", "cycling")


def _licence(**overrides: object) -> dict[str, object]:
    return {
        "license_id": "cc-by",
        "license_source": "pmc",
        "attribution": "B. Schoenfeld et al., Resistance training volume, CC BY 4.0",
        **overrides,
    }


def _item(**overrides: object) -> dict[str, object]:
    """A PMC article as `/stage-knowledge` would send it: CSL-JSON plus the project's fields."""
    return {
        "type": "article-journal",
        "title": "Resistance training volume and hypertrophy: a meta-analysis",
        "author": [{"family": "Schoenfeld", "given": "Brad"}, {"literal": "The NSCA"}],
        "issued": {"date-parts": [[2017, 6]]},
        "DOI": "10.1080/02640414.2016.1210197",
        "PMID": "27433992",
        "PMCID": "PMC5463214",
        "abstract": "Volume matters.",
        "keyword": "hypertrophy, volume",
        "language": "en",
        "URL": "https://pmc.ncbi.nlm.nih.gov/articles/PMC5463214/",
        "container-title": "Journal of Sports Sciences",
        "evidence_tier": "peer_reviewed",
        "source_type": "research_paper",
        "sports": ["gym", "gym"],
        "topics": [" Hypertrophy", "volume", "hypertrophy"],
        "licence": _licence(),
        **overrides,
    }


def _template(**overrides: object) -> SourceTemplate:
    return SourceTemplate.model_validate(_item(**overrides), context={"sports": SPORTS})


def _errors(**overrides: object) -> list[tuple[tuple[str | int, ...], str]]:
    with pytest.raises(ValidationError) as caught:
        _template(**overrides)
    return [(tuple(e["loc"]), e["type"]) for e in caught.value.errors()]


def test_a_csl_json_item_with_the_projects_fields_validates() -> None:
    template = _template()
    assert template.csl_type == "article-journal"
    assert template.doi == "10.1080/02640414.2016.1210197"
    assert (template.pmcid, template.pmid) == ("PMC5463214", "27433992")
    assert template.issued is not None
    assert template.issued.date_parts == ((2017, 6),)
    assert template.container_title == "Journal of Sports Sciences"
    assert template.sports == ("gym",)
    assert template.topics == ("hypertrophy", "volume")
    assert template.licence == LicenceTerms(
        license_id=LicenseId.CC_BY,
        license_source=LicenseSource.PMC,
        attribution="B. Schoenfeld et al., Resistance training volume, CC BY 4.0",
    )


def test_its_details_are_the_fields_every_source_stores() -> None:
    details = _template().details()
    assert details.title == "Resistance training volume and hypertrophy: a meta-analysis"
    assert details.author == "Brad Schoenfeld, The NSCA"
    assert details.source_type is SourceType.RESEARCH_PAPER
    assert details.evidence_tier is EvidenceTier.PEER_REVIEWED
    assert details.language is SourceLanguage.EN
    assert details.source_url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC5463214/"
    assert details.license_note is None


def test_a_template_without_a_licence_is_one_to_detect() -> None:
    template = _template(licence=None)
    assert template.licence is None
    assert template.details().license_note is None


def test_a_doi_in_any_case_reads_as_one_and_blanks_read_as_absent() -> None:
    template = _template(DOI=" 10.1080/ABC ", ISBN="  ", author=[])
    assert (template.doi, template.isbn, template.details().author) == ("10.1080/abc", None, None)


def test_links_may_be_null_or_blank() -> None:
    template = _template(URL=None, licence=_licence(license_url="  "))
    assert template.url is None
    assert template.licence is not None
    assert template.licence.license_url is None


def test_an_unknown_licence_id_is_refused() -> None:
    assert _errors(licence=_licence(license_id="cc-by-4.0")) == [
        (("licence", "license_id"), "enum")
    ]


@pytest.mark.parametrize("refused", ["tdm", "implied-oa", "free-to-read", ""])
def test_licences_off_the_admit_list_are_refused(refused: str) -> None:
    assert _errors(licence=_licence(license_id=refused)) == [(("licence", "license_id"), "enum")]


def test_other_needs_a_note_saying_what_permission_is_held() -> None:
    assert _errors(licence=_licence(license_id="other", license_note="  ")) == [
        (("licence", "license_note"), "missing")
    ]
    terms = _template(licence=_licence(license_id="other", license_note=" Written consent "))
    assert terms.licence is not None
    assert terms.licence.license_note == "Written consent"


def test_a_cc_licence_drops_a_stray_note() -> None:
    template = _template(licence=_licence(license_note="leftover"))
    assert template.licence is not None
    assert template.licence.license_note is None


def test_a_licence_needs_an_attribution() -> None:
    assert _errors(licence=_licence(attribution=" ")) == [
        (("licence", "attribution"), "string_too_short")
    ]


def test_a_licence_url_must_be_a_web_link() -> None:
    assert _errors(licence=_licence(license_url="creativecommons.org/licenses/by/4.0")) == [
        (("licence", "license_url"), "not_a_url")
    ]
    terms = _template(licence=_licence(license_url="https://creativecommons.org/licenses/by/4.0/"))
    assert terms.licence is not None
    assert terms.licence.license_url == "https://creativecommons.org/licenses/by/4.0/"


@pytest.mark.parametrize(
    ("overrides", "loc", "kind"),
    [
        ({"title": "  "}, ("title",), "string_too_short"),
        ({"evidence_tier": "catalogue"}, ("evidence_tier",), "unknown"),
        ({"source_type": "catalogue"}, ("source_type",), "unknown"),
        ({"sports": []}, ("sports",), "too_short"),
        ({"sports": ["gym", "chess"]}, ("sports",), "unknown"),
        ({"URL": "ftp://example.org"}, ("URL",), "not_a_url"),
        ({"PMCID": "5463214"}, ("PMCID",), "string_pattern_mismatch"),
        ({"author": [{}]}, ("author", 0), "missing"),
        ({"issued": {"date-parts": []}}, ("issued", "date-parts"), "too_short"),
        ({"publisher": "Elsevier"}, ("publisher",), "extra_forbidden"),
    ],
    ids=[
        "blank-title",
        "catalogue-tier",
        "catalogue-type",
        "no-sport",
        "unknown-sport",
        "ftp-url",
        "bare-pmcid",
        "nameless-author",
        "dateless-issued",
        "unlisted-field",
    ],
)
def test_a_malformed_item_names_its_field(
    overrides: dict[str, Any], loc: tuple[str | int, ...], kind: str
) -> None:
    assert _errors(**overrides) == [(loc, kind)]


@pytest.mark.parametrize("context", [None, {}], ids=["no-context", "no-sports"])
def test_validating_without_the_registrys_sports_is_a_bug_not_a_refusal(
    context: dict[str, object] | None,
) -> None:
    with pytest.raises(RuntimeError, match="context="):
        SourceTemplate.model_validate(_item(), context=context)


def test_its_details_carry_the_identifiers() -> None:
    details = _template(ISBN="9788312345678").details()
    assert (details.pmcid, details.doi, details.pmid, details.isbn) == (
        "PMC5463214",
        "10.1080/02640414.2016.1210197",
        "27433992",
        "9788312345678",
    )


@pytest.mark.parametrize(
    ("license_id", "flag"),
    [
        (LicenseId.CC0, None),
        (LicenseId.CC_BY, None),
        (LicenseId.CC_BY_NC, None),
        (LicenseId.OTHER, None),
        (None, None),
        (LicenseId.CC_BY_SA, LicenceFlag.SHARE_ALIKE),
        (LicenseId.CC_BY_NC_SA, LicenceFlag.SHARE_ALIKE),
        (LicenseId.CC_BY_ND, LicenceFlag.NO_DERIVATIVES),
        (LicenseId.CC_BY_NC_ND, LicenceFlag.NO_DERIVATIVES),
    ],
)
def test_licence_flag_marks_every_share_alike_and_no_derivatives_licence(
    license_id: LicenseId | None, flag: LicenceFlag | None
) -> None:
    assert licence_flag(license_id) is flag
