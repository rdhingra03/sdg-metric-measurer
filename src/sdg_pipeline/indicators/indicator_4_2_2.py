"""Statistical definition for the legacy U.S. implementation of SDG 4.2.2.

This module interprets already-parsed CPS person observations. It does not
retrieve HTTP responses or understand ZIP, gzip, or fixed-width file formats.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Dict, Mapping, Sequence

from ..archive import ArchiveReadError, read_nested_zip_member
from ..sources.census_cps import CpsObservation
from ..standardized import (
    ARCHIVE_MATCHED,
    ARCHIVE_MISMATCH,
    NOT_ARCHIVE_VALIDATED,
    StandardizedObservation,
)


INDICATOR_ID = "4.2.2"
INDICATOR_TITLE = "Percentage of 5-year-olds enrolled in organized learning"
METHODOLOGY_VARIANT = "legacy_us_cps_age_5_organized_learning"
REQUIRED_PERSON_VARIABLES = ("PRTAGE", "PESCH35", "PECHGRDE", "PESEX")
TARGET_AGE = 5
ENROLLED_CODE = 1
VALID_ENROLLMENT_CODES = {1, 2}
ORGANIZED_LEARNING_GRADES = range(1, 17)
VALID_SEX_CODES = {1, 2}

CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_DATA_PATH = "sdg-master/data/indicator_4-2-2.csv"

AGE_QUALIFICATION = (
    "Age five means age at last birthday at the October CPS interview."
)
PANDEMIC_WARNING = (
    "Census notes that pandemic-era response and enrollment classification "
    "issues may affect the 2020 estimate."
)
METHODOLOGY_NOTE = (
    "The legacy U.S. measure is the survey-weighted percentage of 5-year-olds "
    "enrolled in prekindergarten, kindergarten, or first grade or higher."
)


@dataclass(frozen=True)
class PersonRecord:
    """The five CPS values used by this indicator's statistical definition."""

    age: int
    enrollment: int
    grade: int
    sex: int
    raw_weight: int


@dataclass(frozen=True)
class GroupResult:
    """Weighted and unweighted results for one national or sex group."""

    raw_weighted_numerator: int
    raw_weighted_denominator: int
    unweighted_numerator: int
    unweighted_denominator: int
    calculated_fraction: Fraction


@dataclass(frozen=True)
class YearResult:
    """Calculated national and sex-disaggregated results for one survey year."""

    year: int
    weight_variable: str
    source_url: str
    retrieval_method: str
    national: GroupResult
    male: GroupResult
    female: GroupResult


@dataclass(frozen=True)
class ArchivedValue:
    """One archived value plus its displayed decimal precision."""

    value: Decimal
    decimal_places: int


def person_from_observation(
    observation: CpsObservation, weight_variable: str
) -> PersonRecord:
    """Map named CPS variables into the fields used by this indicator."""

    return PersonRecord(
        age=observation.value("PRTAGE"),
        enrollment=observation.value("PESCH35"),
        grade=observation.value("PECHGRDE"),
        sex=observation.value("PESEX"),
        raw_weight=observation.value(weight_variable),
    )


def select_age_five(
    observations: Sequence[CpsObservation], weight_variable: str
) -> list[PersonRecord]:
    """Select exact age-five observations and map their required CPS fields."""

    return [
        person_from_observation(observation, weight_variable)
        for observation in observations
        if observation.value("PRTAGE") == TARGET_AGE
    ]


def is_enrolled(record: PersonRecord) -> bool:
    """Return whether PESCH35 classifies this person as enrolled."""

    return record.enrollment == ENROLLED_CODE


def validate_records(records: Sequence[PersonRecord], year: int) -> None:
    """Apply the indicator's age, enrollment, grade, and sex consistency rules."""

    if any(record.age != TARGET_AGE for record in records):
        raise RuntimeError(f"{year} retrieval included a person who is not age 5")

    invalid_enrollment = sorted(
        {
            record.enrollment
            for record in records
            if record.enrollment not in VALID_ENROLLMENT_CODES
        }
    )
    if invalid_enrollment:
        raise RuntimeError(
            f"{year} has invalid PESCH35 codes for age-5 records: "
            + ", ".join(map(str, invalid_enrollment))
        )

    invalid_grades = sorted(
        {
            record.grade
            for record in records
            if is_enrolled(record)
            and record.grade not in ORGANIZED_LEARNING_GRADES
        }
    )
    if invalid_grades:
        raise RuntimeError(
            f"{year} has enrolled age-5 records outside the organized-learning "
            "grade range: "
            + ", ".join(map(str, invalid_grades))
        )

    invalid_sex = sorted(
        {record.sex for record in records if record.sex not in VALID_SEX_CODES}
    )
    if invalid_sex:
        raise RuntimeError(
            f"{year} has invalid PESEX codes: "
            + ", ".join(map(str, invalid_sex))
        )


def calculate_group(
    records: Sequence[PersonRecord], label: str, year: int
) -> GroupResult:
    """Calculate one weighted group using only positive survey weights."""

    denominator_records = [record for record in records if record.raw_weight > 0]
    numerator_records = [
        record for record in denominator_records if is_enrolled(record)
    ]
    raw_denominator = sum(record.raw_weight for record in denominator_records)
    raw_numerator = sum(record.raw_weight for record in numerator_records)
    if not denominator_records or raw_denominator <= 0:
        raise RuntimeError(f"{year} {label} has no valid positive-weight records")
    return GroupResult(
        raw_weighted_numerator=raw_numerator,
        raw_weighted_denominator=raw_denominator,
        unweighted_numerator=len(numerator_records),
        unweighted_denominator=len(denominator_records),
        calculated_fraction=Fraction(100 * raw_numerator, raw_denominator),
    )


def calculate(
    year: int,
    records: Sequence[PersonRecord],
    retrieval_method: str,
    source_url: str,
    weight_variable: str,
) -> YearResult:
    """Calculate national, male, and female values for one survey year."""

    validate_records(records, year)
    male_records = [record for record in records if record.sex == 1]
    female_records = [record for record in records if record.sex == 2]
    national = calculate_group(records, "national", year)
    male = calculate_group(male_records, "male", year)
    female = calculate_group(female_records, "female", year)

    if (
        male.raw_weighted_denominator + female.raw_weighted_denominator
        != national.raw_weighted_denominator
        or male.raw_weighted_numerator + female.raw_weighted_numerator
        != national.raw_weighted_numerator
    ):
        raise RuntimeError(f"{year} male and female totals do not reconcile nationally")

    return YearResult(
        year=year,
        weight_variable=weight_variable,
        source_url=source_url,
        retrieval_method=retrieval_method,
        national=national,
        male=male,
        female=female,
    )


def fraction_to_decimal(value: Fraction) -> Decimal:
    """Convert an exact fraction to a high-precision decimal without truncation."""

    with localcontext() as context:
        context.prec = 50
        return Decimal(value.numerator) / Decimal(value.denominator)


def decimal_text(value: Decimal) -> str:
    """Write ordinary decimal notation, trimming insignificant zeros."""

    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def weighted_population_text(raw_weight_sum: int, weight_scale: int) -> str:
    """Convert a raw implied-decimal weight sum to estimated people."""

    value = Decimal(raw_weight_sum) / Decimal(weight_scale)
    return format(value, ".4f")


def group_output_fields(
    group: GroupResult, weight_scale: int
) -> Dict[str, object]:
    """Return the existing audit columns for one calculated group."""

    return {
        "weighted_numerator": weighted_population_text(
            group.raw_weighted_numerator, weight_scale
        ),
        "weighted_denominator": weighted_population_text(
            group.raw_weighted_denominator, weight_scale
        ),
        "unweighted_numerator": group.unweighted_numerator,
        "unweighted_denominator": group.unweighted_denominator,
        "calculated_value": decimal_text(
            fraction_to_decimal(group.calculated_fraction)
        ),
    }


def build_output_rows(
    results: Sequence[YearResult], retrieval_date: str, weight_scale: int
) -> tuple[list[Dict[str, object]], list[Dict[str, object]]]:
    """Build the existing national and sex-disaggregated output rows."""

    national_rows: list[Dict[str, object]] = []
    sex_rows: list[Dict[str, object]] = []
    for result in results:
        shared = {
            "year": result.year,
            "weight_variable": result.weight_variable,
            "source_url": result.source_url,
            "retrieval_method": result.retrieval_method,
            "retrieval_date": retrieval_date,
        }
        national_rows.append(
            {**shared, **group_output_fields(result.national, weight_scale)}
        )
        for sex, group in (("Male", result.male), ("Female", result.female)):
            sex_rows.append(
                {
                    **shared,
                    "sex": sex,
                    **group_output_fields(group, weight_scale),
                }
            )
    return national_rows, sex_rows


def parse_archived_values(csv_text: str) -> Dict[tuple[int, str], ArchivedValue]:
    """Parse national and sex rows using each value's stored precision."""

    archived: Dict[tuple[int, str], ArchivedValue] = {}
    for row in csv.DictReader(io.StringIO(csv_text, newline="")):
        if (row.get("Income") or "").strip():
            continue
        sex = (row.get("Sex") or "").strip() or "National"
        if sex not in {"National", "Male", "Female"}:
            continue
        value_text = (row.get("Value") or "").strip()
        try:
            year = int(row["Year"])
            value = Decimal(value_text)
        except (KeyError, ValueError, ArithmeticError) as error:
            raise RuntimeError(f"Invalid archived SDG row: {row}") from error
        decimal_places = len(value_text.partition(".")[2]) if "." in value_text else 0
        key = (year, sex)
        if key in archived:
            raise RuntimeError(f"Duplicate archived SDG row: {year} {sex}")
        archived[key] = ArchivedValue(value, decimal_places)
    return archived


def read_archived_values(
    archive_path: Path,
) -> Dict[tuple[int, str], ArchivedValue]:
    """Read and parse this indicator's canonical CSV from the legacy archive."""

    try:
        csv_text = read_nested_zip_member(
            archive_path, CANONICAL_ZIP_MEMBER, CANONICAL_DATA_PATH
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "Could not read the archived canonical SDG 4.2.2 CSV"
        ) from error
    return parse_archived_values(csv_text)


def compare_at_archived_precision(
    calculated: Fraction, archived: ArchivedValue
) -> Decimal:
    """Return the difference after matching the archive's displayed precision."""

    quantizer = Decimal(1).scaleb(-archived.decimal_places)
    rounded = fraction_to_decimal(calculated).quantize(
        quantizer, rounding=ROUND_HALF_UP
    )
    return abs(rounded - archived.value)


def validate_against_archive(
    results: Sequence[YearResult],
    archived: Mapping[tuple[int, str], ArchivedValue],
) -> Dict[str, object]:
    """Validate available national and sex values against the legacy archive."""

    national_differences: Dict[int, Decimal] = {}
    sex_differences: Dict[tuple[int, str], Decimal] = {}
    for result in results:
        national_key = (result.year, "National")
        if national_key in archived:
            national_differences[result.year] = compare_at_archived_precision(
                result.national.calculated_fraction, archived[national_key]
            )
        for sex, group in (("Male", result.male), ("Female", result.female)):
            key = (result.year, sex)
            if key in archived:
                sex_differences[key] = compare_at_archived_precision(
                    group.calculated_fraction, archived[key]
                )

    if not national_differences:
        raise RuntimeError("No overlapping national years exist for archive validation")

    national_mismatches = [
        year for year, difference in national_differences.items() if difference != 0
    ]
    sex_mismatches = [
        key for key, difference in sex_differences.items() if difference != 0
    ]
    return {
        "national_overlaps": len(national_differences),
        "national_exact_matches": len(national_differences)
        - len(national_mismatches),
        "national_maximum_difference": max(national_differences.values()),
        "national_mismatches": national_mismatches,
        "sex_overlaps": len(sex_differences),
        "sex_exact_matches": len(sex_differences) - len(sex_mismatches),
        "sex_maximum_difference": max(
            sex_differences.values(), default=Decimal(0)
        ),
        "sex_mismatches": sex_mismatches,
    }


def _standardized_validation_status(
    year: int,
    group_name: str,
    calculated: Fraction,
    archived: Mapping[tuple[int, str], ArchivedValue],
) -> str:
    """Return the per-observation archive status at stored archive precision."""

    key = (year, group_name)
    if key not in archived:
        return NOT_ARCHIVE_VALIDATED
    if compare_at_archived_precision(calculated, archived[key]) == 0:
        return ARCHIVE_MATCHED
    return ARCHIVE_MISMATCH


def build_standardized_observations(
    results: Sequence[YearResult],
    archived: Mapping[tuple[int, str], ArchivedValue],
    retrieval_date: str,
    source_organization: str,
    source_dataset: str,
) -> list[StandardizedObservation]:
    """Translate national and sex results to the common observation schema."""

    observations: list[StandardizedObservation] = []
    for result in results:
        warning = AGE_QUALIFICATION
        if result.year == 2020:
            warning = f"{warning} {PANDEMIC_WARNING}"

        groups = (
            ("National", result.national, {}),
            ("Male", result.male, {"sex": "Male"}),
            ("Female", result.female, {"sex": "Female"}),
        )
        for group_name, group, disaggregation in groups:
            observations.append(
                StandardizedObservation(
                    indicator_id=INDICATOR_ID,
                    indicator_title=INDICATOR_TITLE,
                    year=result.year,
                    value=decimal_text(
                        fraction_to_decimal(group.calculated_fraction)
                    ),
                    unit="percent",
                    geography="United States",
                    disaggregation=disaggregation,
                    source_organization=source_organization,
                    source_dataset=source_dataset,
                    source_url=result.source_url,
                    retrieval_method=result.retrieval_method,
                    retrieval_date=retrieval_date,
                    methodology_variant=METHODOLOGY_VARIANT,
                    validation_status=_standardized_validation_status(
                        result.year,
                        group_name,
                        group.calculated_fraction,
                        archived,
                    ),
                    data_warning=warning,
                )
            )
    return observations
