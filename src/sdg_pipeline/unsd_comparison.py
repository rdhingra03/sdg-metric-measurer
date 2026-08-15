"""Reviewed UNSD series selection and comparison-output construction.

The source connector retrieves observations without making statistical
choices.  This module is the deliberately small, human-reviewed layer that
states which series and dimensions are meaningful comparisons for the current
U.S. headline calculations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import SourceValidationError
from .output import write_csv_atomically
from .sources import unsd


DIRECTLY_COMPARABLE = "directly_comparable"
METHODOLOGY_DIFFERENCE = "comparable_with_methodology_difference"
PARTIAL_COMPONENT = "partial_component_comparison"

COMPARISON_AND_FALLBACK = "comparison_and_fallback"
COMPARISON_ONLY = "comparison_only"

# Fallback eligibility is never the same thing as a source refresh failure.
# Future card/runner code must consult both this global policy and the reviewed
# per-series ``fallback_suitability`` value before displaying a UN fallback.
FALLBACK_POLICY = {
    "underlying_us_data_unavailable": "use_reviewed_unsd_fallback_when_suitability_allows",
    "us_pipeline_refresh_failed": "retain_last_successful_us_observation",
}

LATEST_AVAILABLE = "latest_available"
LATEST_REVIEWED_COMPLETE = "latest_reviewed_complete"
LATEST_NONPROVISIONAL = "latest_nonprovisional"

COMPARISON_COLUMNS = [
    "indicator_id",
    "comparison_component",
    "series_code",
    "series_description",
    "year",
    "value",
    "unit",
    "geography",
    "geography_code",
    "disaggregation",
    "nature_code",
    "nature_description",
    "observation_status",
    "source_organization",
    "source_dataset",
    "reported_source",
    "source_url",
    "custodian",
    "database_release",
    "database_last_updated",
    "comparison_status",
    "fallback_suitability",
    "completeness_status",
    "is_preferred_comparison",
    "retrieval_method",
    "retrieval_date",
    "footnotes",
    "notes",
]


@dataclass(frozen=True)
class SelectionRule:
    """One reviewed series/component/dimension selection."""

    indicator_id: str
    series_code: str
    comparison_component: str
    required_dimensions: Mapping[str, str]
    accepted_nature_codes: frozenset[str]
    comparison_status: str
    fallback_suitability: str
    completeness_rule: str
    custodian: str
    notes: str
    known_incomplete_years: tuple[int, ...] = ()


def _rule(
    indicator_id: str,
    series_code: str,
    component: str,
    dimensions: Mapping[str, str],
    nature: str,
    comparison_status: str,
    fallback_suitability: str,
    custodian: str,
    notes: str,
    *,
    completeness_rule: str = LATEST_AVAILABLE,
    known_incomplete_years: tuple[int, ...] = (),
) -> SelectionRule:
    """Keep the registry below compact without hiding any reviewed fields."""

    return SelectionRule(
        indicator_id=indicator_id,
        series_code=series_code,
        comparison_component=component,
        required_dimensions=dict(dimensions),
        accepted_nature_codes=frozenset({nature}),
        comparison_status=comparison_status,
        fallback_suitability=fallback_suitability,
        completeness_rule=completeness_rule,
        custodian=custodian,
        notes=notes,
        known_incomplete_years=known_incomplete_years,
    )


GLOBAL = {"Reporting Type": "G"}
WHO = "World Health Organization (WHO)"
OECD = "Organisation for Economic Co-operation and Development (OECD)"
ILO = "International Labour Organization (ILO)"

SELECTION_REGISTRY = (
    _rule(
        "3.1.2", "SH_STA_BRTC", "national", GLOBAL, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK,
        "United Nations Children's Fund (UNICEF)",
        "Country-reported skilled birth-attendance percentage; the API source may be an MNCH source code.",
    ),
    _rule(
        "3.4.2", "SH_STA_SCIDE", "both_sexes", {**GLOBAL, "Sex": "BOTHSEX"}, "CA",
        METHODOLOGY_DIFFERENCE, COMPARISON_ONLY, WHO,
        "WHO country-adjusted Global Health Estimates are compared with the project's crude NVSS rate.",
    ),
    _rule(
        "3.6.1", "SH_STA_TRAF", "national_rate", GLOBAL, "C",
        METHODOLOGY_DIFFERENCE, COMPARISON_ONLY, WHO,
        "The UN road-safety rate may combine police, health, transport, and adjustment processes; the separate death-count series is not selected.",
    ),
    _rule(
        "3.7.2", "SP_DYN_ADKL", "ages_10_14",
        {**GLOBAL, "Age": "10-14", "Sex": "FEMALE", "Location": "ALLAREA"}, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK,
        "United Nations Department of Economic and Social Affairs, Population Division",
        "Age-specific adolescent birth rate for women ages 10-14.",
    ),
    _rule(
        "3.7.2", "SP_DYN_ADKL", "ages_15_19",
        {**GLOBAL, "Age": "15-19", "Sex": "FEMALE", "Location": "ALLAREA"}, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK,
        "United Nations Department of Economic and Social Affairs, Population Division",
        "Age-specific adolescent birth rate for women ages 15-19.",
    ),
    _rule(
        "3.9.3", "SH_STA_POISN", "both_sexes", {**GLOBAL, "Sex": "BOTHSEX"}, "CA",
        METHODOLOGY_DIFFERENCE, COMPARISON_ONLY, WHO,
        "WHO country-adjusted Global Health Estimates are compared with the project's crude NVSS rate and current ICD selection.",
    ),
    _rule(
        "4.2.2", "SE_PRE_PARTN", "both_sexes", {**GLOBAL, "Sex": "BOTHSEX"}, "C",
        METHODOLOGY_DIFFERENCE, COMPARISON_ONLY,
        "UNESCO Institute for Statistics (UIS)",
        "The UOE country submission is compared with the project's weighted CPS age-five estimate.",
    ),
    _rule(
        "8.5.2", "SL_TLF_UEMDIS_19ICLS", "male_with_disability",
        {**GLOBAL, "Age": "15+", "Sex": "MALE", "Disability status": "PD"}, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK, ILO,
        "Current 19th-ICLS series; the U.S. source footnote records a minimum age of 16.",
    ),
    _rule(
        "8.5.2", "SL_TLF_UEMDIS_19ICLS", "female_with_disability",
        {**GLOBAL, "Age": "15+", "Sex": "FEMALE", "Disability status": "PD"}, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK, ILO,
        "Current 19th-ICLS series; the U.S. source footnote records a minimum age of 16.",
    ),
    _rule(
        "8.5.2", "SL_TLF_UEMDIS_19ICLS", "male_without_disability",
        {**GLOBAL, "Age": "15+", "Sex": "MALE", "Disability status": "PWD"}, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK, ILO,
        "Current 19th-ICLS series; PWD is UNSD's code for persons without disability.",
    ),
    _rule(
        "8.5.2", "SL_TLF_UEMDIS_19ICLS", "female_without_disability",
        {**GLOBAL, "Age": "15+", "Sex": "FEMALE", "Disability status": "PWD"}, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK, ILO,
        "Current 19th-ICLS series; PWD is UNSD's code for persons without disability.",
    ),
    _rule(
        "8.6.1", "SL_TLF_NEET_19ICLS", "both_sexes_ages_15_24",
        {**GLOBAL, "Age": "15-24", "Sex": "BOTHSEX"}, "C",
        METHODOLOGY_DIFFERENCE, COMPARISON_ONLY, ILO,
        "Official NEET measure differs from the legacy U.S. ages 16-24 not-enrolled/not-employed proxy.",
    ),
    _rule(
        "8.a.1", "DC_TOF_TRDCMDL", "donor_commitments", GLOBAL, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK, OECD,
        "United States donor Aid for Trade commitments in millions of constant 2024 USD.",
    ),
    _rule(
        "8.a.1", "DC_TOF_TRDDBMDL", "donor_disbursements", GLOBAL, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK, OECD,
        "United States donor Aid for Trade gross disbursements in millions of constant 2024 USD.",
    ),
    _rule(
        "10.b.1", "DC_TRF_TOTDL", "donor_assistance", GLOBAL, "C",
        DIRECTLY_COMPARABLE, COMPARISON_AND_FALLBACK, OECD,
        "The reviewed 2025 observation appears incomplete; prefer the latest reviewed-complete year.",
        completeness_rule=LATEST_REVIEWED_COMPLETE,
        known_incomplete_years=(2025,),
    ),
    _rule(
        "15.a.1", "DC_ODA_BDVDL", "donor_biodiversity_oda_component_a", GLOBAL, "C",
        PARTIAL_COMPONENT, COMPARISON_AND_FALLBACK, OECD,
        "Comparable only to component (a), donor biodiversity-related ODA; component (b) is not present in this series.",
    ),
    _rule(
        "15.b.1", "DC_ODA_BDVDL", "donor_biodiversity_oda_component_a", GLOBAL, "C",
        PARTIAL_COMPONENT, COMPARISON_AND_FALLBACK, OECD,
        "Repeat of 15.a.1 for component (a); both indicators intentionally share one retrieved series.",
    ),
    _rule(
        "17.2.1", "DC_ODA_TOTGGE", "total_oda_gni_grant_equivalent", GLOBAL, "C",
        METHODOLOGY_DIFFERENCE, COMPARISON_ONLY, OECD,
        "Current UN total uses grant equivalent after 2018; the project's U.S. total deliberately uses net ODA.",
        completeness_rule=LATEST_NONPROVISIONAL,
    ),
    _rule(
        "17.2.1", "DC_ODA_LDCG", "ldc_oda_gni_net", GLOBAL, "C",
        METHODOLOGY_DIFFERENCE, COMPARISON_ONLY, OECD,
        "Net ODA to least developed countries including imputed multilateral ODA; directly comparable within this mixed-method indicator.",
        completeness_rule=LATEST_NONPROVISIONAL,
    ),
)


def indicator_ids(registry: Sequence[SelectionRule] = SELECTION_REGISTRY) -> tuple[str, ...]:
    """Return each configured indicator once in stable goal/registry order."""

    return tuple(dict.fromkeys(rule.indicator_id for rule in registry))


def rules_for_indicator(
    indicator_id: str,
    registry: Sequence[SelectionRule] = SELECTION_REGISTRY,
) -> tuple[SelectionRule, ...]:
    """Return every reviewed component rule for one indicator."""

    return tuple(rule for rule in registry if rule.indicator_id == indicator_id)


def serialize_json(value: object) -> str:
    """Return stable compact JSON for CSV fields."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _matches(observation: unsd.UnsdObservation, rule: SelectionRule) -> bool:
    """Return whether one generic observation satisfies a reviewed rule."""

    return (
        rule.indicator_id in observation.indicator_ids
        and observation.series_code == rule.series_code
        and observation.attributes.get("Nature", "") in rule.accepted_nature_codes
        and all(
            observation.dimensions.get(name) == code
            for name, code in rule.required_dimensions.items()
        )
    )


def _completeness_status(
    observation: unsd.UnsdObservation, rule: SelectionRule
) -> str:
    """Apply only the explicit reviewed finance-completeness rules."""

    if observation.year in rule.known_incomplete_years:
        return "apparently_incomplete"
    if any("provisional" in footnote.lower() for footnote in observation.footnotes):
        return "provisional"
    if rule.completeness_rule in {LATEST_REVIEWED_COMPLETE, LATEST_NONPROVISIONAL}:
        return "complete"
    return "not_assessed"


def _eligible_for_preferred(status: str, rule: SelectionRule) -> bool:
    """Return whether a row may be the preferred comparison observation."""

    if rule.completeness_rule == LATEST_REVIEWED_COMPLETE:
        return status == "complete"
    if rule.completeness_rule == LATEST_NONPROVISIONAL:
        return status != "provisional"
    return True


def _string_value(observation: unsd.UnsdObservation) -> str:
    """Preserve the API's decimal precision without scientific notation."""

    return format(observation.value, "f")


def build_comparison_rows(
    result: unsd.UnsdResult,
    registry: Sequence[SelectionRule] = SELECTION_REGISTRY,
) -> dict[str, list[dict[str, object]]]:
    """Select reviewed observations and build deterministic per-indicator rows."""

    rows_by_indicator: dict[str, list[dict[str, object]]] = {
        indicator_id: [] for indicator_id in indicator_ids(registry)
    }
    seen_keys: set[tuple[object, ...]] = set()

    for rule in registry:
        observations = [
            observation for observation in result.observations if _matches(observation, rule)
        ]
        if not observations:
            raise SourceValidationError(
                f"UNSD returned no reviewed observations for {rule.indicator_id} "
                f"component {rule.comparison_component!r} ({rule.series_code})"
            )
        statuses = {
            id(observation): _completeness_status(observation, rule)
            for observation in observations
        }
        eligible_years = [
            observation.year
            for observation in observations
            if _eligible_for_preferred(statuses[id(observation)], rule)
        ]
        if not eligible_years:
            raise SourceValidationError(
                f"No complete UNSD comparison remains for {rule.indicator_id} "
                f"component {rule.comparison_component!r}"
            )
        preferred_year = max(eligible_years)

        for observation in observations:
            identity = (
                rule.indicator_id,
                rule.comparison_component,
                observation.series_code,
                observation.year,
                tuple(sorted(observation.dimensions.items())),
            )
            if identity in seen_keys:
                raise SourceValidationError(
                    "UNSD selection produced conflicting or duplicate comparison rows for "
                    f"{rule.indicator_id} {rule.comparison_component} {observation.year}"
                )
            seen_keys.add(identity)
            nature_code = observation.attributes.get("Nature", "")
            unit_code = observation.attributes.get("Units", "")
            source_dataset = observation.series_description
            row = {
                "indicator_id": rule.indicator_id,
                "comparison_component": rule.comparison_component,
                "series_code": observation.series_code,
                "series_description": observation.series_description,
                "year": observation.year,
                "value": _string_value(observation),
                "unit": result.attribute_description("Units", unit_code) or unit_code,
                "geography": observation.geography_name,
                "geography_code": observation.geography_code,
                "disaggregation": serialize_json(dict(observation.dimensions)),
                "nature_code": nature_code,
                "nature_description": (
                    result.attribute_description("Nature", nature_code) or nature_code
                ),
                "observation_status": observation.attributes.get("Observation Status", ""),
                "source_organization": rule.custodian,
                "source_dataset": source_dataset,
                "reported_source": observation.source,
                "source_url": unsd.series_data_url(
                    observation.series_code, observation.geography_code
                ),
                "custodian": rule.custodian,
                "database_release": result.database_release,
                "database_last_updated": result.database_last_updated,
                "comparison_status": rule.comparison_status,
                "fallback_suitability": rule.fallback_suitability,
                "completeness_status": statuses[id(observation)],
                "is_preferred_comparison": (
                    "true"
                    if observation.year == preferred_year
                    and _eligible_for_preferred(statuses[id(observation)], rule)
                    else "false"
                ),
                "retrieval_method": result.retrieval_method,
                "retrieval_date": result.retrieval_date,
                "footnotes": serialize_json(list(observation.footnotes)),
                "notes": rule.notes,
            }
            rows_by_indicator[rule.indicator_id].append(row)

    for rows in rows_by_indicator.values():
        rows.sort(
            key=lambda row: (
                str(row["comparison_component"]),
                int(row["year"]),
                str(row["disaggregation"]),
            )
        )
    return rows_by_indicator


def validate_comparison_row(row: Mapping[str, object]) -> dict[str, object]:
    """Require the exact comparison schema and valid deterministic JSON fields."""

    values = dict(row)
    missing = [column for column in COMPARISON_COLUMNS if column not in values]
    extra = [column for column in values if column not in COMPARISON_COLUMNS]
    if missing or extra:
        details = []
        if missing:
            details.append("missing columns: " + ", ".join(missing))
        if extra:
            details.append("unexpected columns: " + ", ".join(extra))
        raise ValueError("Invalid comparison observation (" + "; ".join(details) + ")")
    if any(value is None for value in values.values()):
        raise ValueError("Comparison observations cannot contain null values")
    for field, expected_type in (("disaggregation", dict), ("footnotes", list)):
        try:
            parsed = json.loads(str(values[field]))
        except json.JSONDecodeError as error:
            raise ValueError(f"{field} must contain valid JSON") from error
        if not isinstance(parsed, expected_type):
            raise ValueError(f"{field} has the wrong JSON type")
        if str(values[field]) != serialize_json(parsed):
            raise ValueError(f"{field} JSON is not deterministic")
    return {column: values[column] for column in COMPARISON_COLUMNS}


def write_comparison_csv(
    output_path: Path, rows: Sequence[Mapping[str, object]]
) -> None:
    """Validate and atomically write one comparison CSV."""

    write_csv_atomically(
        output_path,
        COMPARISON_COLUMNS,
        [validate_comparison_row(row) for row in rows],
    )
