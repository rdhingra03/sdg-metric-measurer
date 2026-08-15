#!/usr/bin/env python3
"""Build a self-contained HTML preview from standardized SDG outputs.

The generated page contains no manually entered observations. Every displayed
year, value, title, unit, source, status, and warning is read from the existing
CSV files in ``data_processed/standardized``. Reviewed comparison values come
from ``data_processed/comparison``. Because both data layers are embedded at
build time, the finished page can be opened directly from the local filesystem
without running a web server.
"""

from __future__ import annotations

import csv
import html
import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import List, Mapping, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STANDARDIZED_DIR = PROJECT_ROOT / "data_processed" / "standardized"
COMPARISON_DIR = PROJECT_ROOT / "data_processed" / "comparison"
OUTPUT_PATH = PROJECT_ROOT / "output" / "data_cards" / "index.html"

INDICATOR_ORDER = (
    "3.1.2",
    "3.4.2",
    "3.6.1",
    "3.7.2",
    "3.9.3",
    "4.2.2",
    "8.5.2",
    "8.6.1",
    "8.a.1",
    "10.b.1",
    "15.a.1",
    "15.b.1",
    "17.2.1",
)

INPUT_FILES = {
    indicator_id: STANDARDIZED_DIR
    / f"sdg_{indicator_id.replace('.', '_')}.csv"
    for indicator_id in INDICATOR_ORDER
}

COMPARISON_FILES = {
    indicator_id: COMPARISON_DIR
    / f"sdg_{indicator_id.replace('.', '_')}.csv"
    for indicator_id in INDICATOR_ORDER
}

# Each entry lines up, in order, with the U.S. metrics selected below. These
# are semantic component names from the reviewed comparison layer—not values.
# Keeping the matching rules here prevents similarly named series from being
# paired merely because they happen to have a recent year.
COMPARISON_COMPONENTS = {
    "3.1.2": ("national",),
    "3.4.2": ("both_sexes",),
    "3.6.1": ("national_rate",),
    "3.7.2": ("ages_15_19", "ages_10_14"),
    "3.9.3": ("both_sexes",),
    "4.2.2": ("both_sexes",),
    "8.5.2": (
        "male_with_disability",
        "female_with_disability",
        "male_without_disability",
        "female_without_disability",
    ),
    "8.6.1": ("both_sexes_ages_15_24",),
    "8.a.1": ("donor_commitments", "donor_disbursements"),
    "10.b.1": ("donor_assistance",),
    "15.a.1": ("donor_biodiversity_oda_component_a",),
    "15.b.1": ("donor_biodiversity_oda_component_a",),
    "17.2.1": ("total_oda_gni_grant_equivalent", "ldc_oda_gni_net"),
}

COMPARISON_LABELS = {
    "directly_comparable": "Comparable measure",
    "comparable_with_methodology_difference": "Methodology differs",
    "partial_component_comparison": "Component comparison",
}

# Only presentation rules live here. The observations themselves always come
# from the standardized CSVs. This is the single source of truth for the
# official United Nations SDG goal colors (web/RGB hex values) used by cards.
GOAL_COLORS = {
    "1": "#E5243B",
    "2": "#DDA63A",
    "3": "#4C9F38",
    "4": "#C5192D",
    "5": "#FF3A21",
    "6": "#26BDE2",
    "7": "#FCC30B",
    "8": "#A21942",
    "9": "#FD6925",
    "10": "#DD1367",
    "11": "#FD9D24",
    "12": "#BF8B2E",
    "13": "#3F7E44",
    "14": "#0A97D9",
    "15": "#56C02B",
    "16": "#00689D",
    "17": "#19486A",
}

REQUIRED_FIELDS = {
    "indicator_id",
    "indicator_title",
    "year",
    "value",
    "unit",
    "geography",
    "disaggregation",
    "source_organization",
    "source_url",
    "validation_status",
    "data_warning",
}

COMPARISON_REQUIRED_FIELDS = {
    "indicator_id",
    "comparison_component",
    "year",
    "value",
    "unit",
    "geography",
    "disaggregation",
    "source_url",
    "custodian",
    "comparison_status",
    "completeness_status",
    "is_preferred_comparison",
    "notes",
}

DISALLOWED_COMPLETENESS = {"apparently_incomplete", "provisional"}


@dataclass(frozen=True)
class Observation:
    """One parsed row from a standardized indicator CSV."""

    row: Mapping[str, str]
    year: int
    value: Decimal
    disaggregation: Mapping[str, str]


@dataclass(frozen=True)
class ComparisonObservation:
    """One reviewed, preferred observation from the UN comparison layer."""

    row: Mapping[str, str]
    component: str
    year: int
    value: Decimal
    disaggregation: Mapping[str, str]


@dataclass(frozen=True)
class Metric:
    """One headline value shown within an indicator card."""

    label: str
    observation: Observation
    comparison: ComparisonObservation | None = None


@dataclass(frozen=True)
class Card:
    """All data needed to render one indicator card."""

    indicator_id: str
    goal: str
    title: str
    metrics: Tuple[Metric, ...]
    source_organization: str
    source_url: str
    validation_status: str
    warnings: Tuple[str, ...]
    comparison_status: str = ""
    comparison_label: str = ""
    comparison_source_url: str = ""
    comparison_custodians: Tuple[str, ...] = ()
    comparison_notes: Tuple[str, ...] = ()


def read_observations(indicator_id: str, path: Path) -> List[Observation]:
    """Read and validate one standardized CSV."""

    if not path.is_file():
        raise FileNotFoundError(f"Missing standardized output: {path}")

    observations: List[Observation] = []
    with path.open("r", encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        missing_fields = sorted(REQUIRED_FIELDS - set(reader.fieldnames or []))
        if missing_fields:
            raise ValueError(
                f"{path.name} is missing required columns: "
                + ", ".join(missing_fields)
            )

        for line_number, row in enumerate(reader, start=2):
            if row["indicator_id"].strip() != indicator_id:
                raise ValueError(
                    f"{path.name}:{line_number} contains indicator "
                    f"{row['indicator_id']!r}; expected {indicator_id!r}"
                )
            try:
                year = int(row["year"])
                value = Decimal(row["value"])
            except (ValueError, InvalidOperation) as error:
                raise ValueError(
                    f"{path.name}:{line_number} has an invalid year or value"
                ) from error
            try:
                disaggregation = json.loads(row["disaggregation"] or "{}")
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path.name}:{line_number} has invalid disaggregation JSON"
                ) from error
            if not isinstance(disaggregation, dict):
                raise ValueError(
                    f"{path.name}:{line_number} disaggregation must be an object"
                )
            observations.append(
                Observation(
                    row=row,
                    year=year,
                    value=value,
                    disaggregation=disaggregation,
                )
            )

    if not observations:
        raise ValueError(f"{path.name} contains no observations")
    return observations


def read_comparison_observations(
    indicator_id: str, path: Path
) -> List[ComparisonObservation]:
    """Read only reviewed preferred rows, rejecting unsafe selections.

    The comparison builder may retain newer incomplete or provisional records
    for audit purposes. Cards must never select those rows. A preferred flag on
    such a row is treated as a data error rather than silently displayed.
    """

    if not path.is_file():
        raise FileNotFoundError(f"Missing comparison output: {path}")

    preferred: List[ComparisonObservation] = []
    with path.open("r", encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        missing_fields = sorted(
            COMPARISON_REQUIRED_FIELDS - set(reader.fieldnames or [])
        )
        if missing_fields:
            raise ValueError(
                f"{path.name} is missing required comparison columns: "
                + ", ".join(missing_fields)
            )

        for line_number, row in enumerate(reader, start=2):
            if row["indicator_id"].strip() != indicator_id:
                raise ValueError(
                    f"{path.name}:{line_number} contains indicator "
                    f"{row['indicator_id']!r}; expected {indicator_id!r}"
                )
            preferred_value = row["is_preferred_comparison"].strip().lower()
            if preferred_value not in {"true", "false"}:
                raise ValueError(
                    f"{path.name}:{line_number} has an invalid preferred flag"
                )
            if preferred_value == "false":
                continue

            completeness = row["completeness_status"].strip().lower()
            if completeness in DISALLOWED_COMPLETENESS:
                raise ValueError(
                    f"{path.name}:{line_number} marks a {completeness} row as "
                    "the preferred card comparison"
                )
            try:
                year = int(row["year"])
                value = Decimal(row["value"])
            except (ValueError, InvalidOperation) as error:
                raise ValueError(
                    f"{path.name}:{line_number} has an invalid year or value"
                ) from error
            try:
                disaggregation = json.loads(row["disaggregation"] or "{}")
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path.name}:{line_number} has invalid disaggregation JSON"
                ) from error
            if not isinstance(disaggregation, dict):
                raise ValueError(
                    f"{path.name}:{line_number} disaggregation must be an object"
                )
            preferred.append(
                ComparisonObservation(
                    row=row,
                    component=row["comparison_component"].strip(),
                    year=year,
                    value=value,
                    disaggregation=disaggregation,
                )
            )

    if not preferred:
        raise ValueError(f"{path.name} has no preferred comparison observations")
    return preferred


def latest_matching(
    observations: Sequence[Observation],
    expected_disaggregation: Mapping[str, str],
) -> Observation:
    """Return the newest observation for one exact disaggregation."""

    matches = [
        observation
        for observation in observations
        if observation.disaggregation == expected_disaggregation
    ]
    if not matches:
        raise ValueError(
            "No observation found for disaggregation "
            + json.dumps(expected_disaggregation, sort_keys=True)
        )
    latest_year = max(observation.year for observation in matches)
    latest = [observation for observation in matches if observation.year == latest_year]
    if len(latest) != 1:
        raise ValueError(
            f"Expected one latest observation for {expected_disaggregation}; "
            f"found {len(latest)} in {latest_year}"
        )
    return latest[0]


def select_metrics(
    indicator_id: str, observations: Sequence[Observation]
) -> Tuple[Metric, ...]:
    """Apply the small set of card-specific latest-value selection rules."""

    if indicator_id == "8.a.1":
        return (
            Metric(
                "Commitments",
                latest_matching(observations, {"flow": "Commitments"}),
            ),
            Metric(
                "Disbursements",
                latest_matching(observations, {"flow": "Disbursements"}),
            ),
        )
    if indicator_id == "17.2.1":
        return (
            Metric(
                "Total ODA",
                latest_matching(observations, {"component": "Total ODA"}),
            ),
            Metric(
                "Least developed countries",
                latest_matching(
                    observations, {"component": "Least developed countries"}
                ),
            ),
        )
    if indicator_id == "8.5.2":
        return (
            Metric(
                "Men with disability",
                latest_matching(
                    observations,
                    {"sex": "Male", "disability": "With disability"},
                ),
            ),
            Metric(
                "Women with disability",
                latest_matching(
                    observations,
                    {"sex": "Female", "disability": "With disability"},
                ),
            ),
            Metric(
                "Men without disability",
                latest_matching(
                    observations,
                    {"sex": "Male", "disability": "No disability"},
                ),
            ),
            Metric(
                "Women without disability",
                latest_matching(
                    observations,
                    {"sex": "Female", "disability": "No disability"},
                ),
            ),
        )
    if indicator_id == "3.7.2":
        return (
            Metric(
                "Ages 15-19",
                latest_matching(observations, {"age": "15-19"}),
            ),
            Metric(
                "Ages 10-14",
                latest_matching(observations, {"age": "10-14"}),
            ),
        )

    # An empty disaggregation object is the national headline series. This is
    # especially important for 4.2.2, whose CSV also contains male/female rows.
    label = "National" if indicator_id == "4.2.2" else "Latest observation"
    return (Metric(label, latest_matching(observations, {})),)


def attach_comparisons(
    indicator_id: str,
    metrics: Sequence[Metric],
    comparisons: Sequence[ComparisonObservation],
) -> Tuple[Metric, ...]:
    """Pair each U.S. metric with its explicitly reviewed UN component."""

    expected_components = COMPARISON_COMPONENTS.get(indicator_id)
    if expected_components is None:
        raise ValueError(f"No comparison-component rules for {indicator_id}")
    if len(expected_components) != len(metrics):
        raise ValueError(
            f"Indicator {indicator_id} has {len(metrics)} U.S. metrics but "
            f"{len(expected_components)} comparison-component rules"
        )

    attached = []
    for metric, component in zip(metrics, expected_components):
        matches = [item for item in comparisons if item.component == component]
        if len(matches) != 1:
            raise ValueError(
                f"Indicator {indicator_id} requires exactly one preferred "
                f"comparison for {component!r}; found {len(matches)}"
            )
        attached.append(Metric(metric.label, metric.observation, matches[0]))

    unexpected = sorted(
        {item.component for item in comparisons} - set(expected_components)
    )
    if unexpected:
        raise ValueError(
            f"Indicator {indicator_id} has unexpected preferred comparison "
            f"components: {', '.join(unexpected)}"
        )
    return tuple(attached)


def one_value(values: Sequence[str], field_name: str, indicator_id: str) -> str:
    """Require a shared descriptive field to agree across selected rows."""

    unique = {value.strip() for value in values if value.strip()}
    if len(unique) != 1:
        raise ValueError(
            f"Indicator {indicator_id} has inconsistent or missing {field_name}: "
            + repr(sorted(unique))
        )
    return unique.pop()


def build_card(
    indicator_id: str,
    observations: Sequence[Observation],
    comparisons: Sequence[ComparisonObservation] = (),
) -> Card:
    metrics = select_metrics(indicator_id, observations)
    if comparisons:
        metrics = attach_comparisons(indicator_id, metrics, comparisons)
    selected_rows = [metric.observation.row for metric in metrics]
    title = one_value(
        [row["indicator_title"] for row in selected_rows],
        "indicator_title",
        indicator_id,
    )
    source_organization = one_value(
        [row["source_organization"] for row in selected_rows],
        "source_organization",
        indicator_id,
    )
    validation_statuses = sorted(
        {
            row["validation_status"].strip()
            for row in selected_rows
            if row["validation_status"].strip()
        }
    )
    if not validation_statuses:
        raise ValueError(f"Indicator {indicator_id} has no validation status")

    source_urls = [row["source_url"].strip() for row in selected_rows]
    first_source_url = next((url.split(" | ", 1)[0] for url in source_urls if url), "")
    warnings = tuple(
        sorted(
            {
                row["data_warning"].strip()
                for row in selected_rows
                if row["data_warning"].strip()
            }
        )
    )

    comparison_status = ""
    comparison_label = ""
    comparison_source_url = ""
    comparison_custodians: Tuple[str, ...] = ()
    comparison_notes: Tuple[str, ...] = ()
    selected_comparisons = [
        metric.comparison for metric in metrics if metric.comparison is not None
    ]
    if selected_comparisons:
        comparison_status = one_value(
            [item.row["comparison_status"] for item in selected_comparisons],
            "comparison_status",
            indicator_id,
        )
        if comparison_status not in COMPARISON_LABELS:
            raise ValueError(
                f"Indicator {indicator_id} has unknown comparison status "
                f"{comparison_status!r}"
            )
        comparison_label = COMPARISON_LABELS[comparison_status]
        if indicator_id in {"15.a.1", "15.b.1"}:
            comparison_label = "ODA component comparison"
        comparison_urls = [
            item.row["source_url"].strip() for item in selected_comparisons
        ]
        comparison_source_url = next(
            (url.split(" | ", 1)[0] for url in comparison_urls if url), ""
        )
        comparison_custodians = tuple(
            sorted(
                {
                    item.row["custodian"].strip()
                    for item in selected_comparisons
                    if item.row["custodian"].strip()
                }
            )
        )
        comparison_notes = tuple(
            dict.fromkeys(
                item.row["notes"].strip()
                for item in selected_comparisons
                if item.row["notes"].strip()
            )
        )
    goal = goal_for_indicator(indicator_id)
    return Card(
        indicator_id=indicator_id,
        goal=goal,
        title=title,
        metrics=metrics,
        source_organization=source_organization,
        source_url=first_source_url,
        validation_status=" / ".join(validation_statuses),
        warnings=warnings,
        comparison_status=comparison_status,
        comparison_label=comparison_label,
        comparison_source_url=comparison_source_url,
        comparison_custodians=comparison_custodians,
        comparison_notes=comparison_notes,
    )


def goal_for_indicator(indicator_id: str) -> str:
    """Derive and validate the SDG goal number from an indicator ID."""

    goal = indicator_id.split(".", 1)[0]
    if goal not in GOAL_COLORS:
        raise ValueError(
            f"Indicator {indicator_id!r} does not begin with an SDG goal from 1 to 17"
        )
    return goal


def readable_text_color(background: str) -> str:
    """Choose dark or white text for readable labels on an SDG goal color."""

    red, green, blue = (
        int(background[index : index + 2], 16) / 255
        for index in (1, 3, 5)
    )

    def linearize(channel: float) -> float:
        if channel <= 0.04045:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    luminance = (
        0.2126 * linearize(red)
        + 0.7152 * linearize(green)
        + 0.0722 * linearize(blue)
    )
    contrast_with_dark = (luminance + 0.05) / 0.05
    contrast_with_white = 1.05 / (luminance + 0.05)
    return "#15212b" if contrast_with_dark >= contrast_with_white else "#ffffff"


def format_value(value: Decimal, unit: str) -> Tuple[str, str]:
    """Format a card value without changing the value stored in the CSV."""

    unit_lower = unit.lower()
    if "percent" in unit_lower:
        places = Decimal("0.001") if abs(value) < 1 else Decimal("0.01")
        rounded = value.quantize(places, rounding=ROUND_HALF_UP)
        display = f"{rounded:f}".rstrip("0").rstrip(".")
        return display, "%"

    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.2f}", ""


def format_difference(metric: Metric) -> Tuple[str, str] | None:
    """Return a signed U.S.-minus-UN difference in a meaningful unit.

    This intentionally calculates an absolute difference, never a relative
    percent change. A difference is omitted when the two units are not clearly
    compatible.
    """

    comparison = metric.comparison
    if comparison is None:
        return None
    # A cross-year subtraction can look like a source disagreement when it may
    # simply reflect real change over time. Show both years, but only calculate
    # a difference when the observations refer to the same year.
    if metric.observation.year != comparison.year:
        return None
    us_unit = metric.observation.row["unit"].lower()
    un_unit = comparison.row["unit"].lower()
    if "percent" in us_unit and "percent" in un_unit:
        difference_unit = "percentage points"
    elif "per 100,000" in us_unit and "per 100,000" in un_unit:
        difference_unit = "rate points"
    elif "per 1,000" in us_unit and "per 1,000" in un_unit:
        difference_unit = "rate points"
    elif "million" in us_unit and "million" in un_unit:
        if ("constant" in us_unit) != ("constant" in un_unit):
            return None
        if ("current" in us_unit) != ("current" in un_unit):
            return None
        difference_unit = "million USD"
    else:
        return None

    difference = metric.observation.value - comparison.value
    absolute = abs(difference)
    if absolute == 0:
        places = Decimal("0.1")
    elif absolute < Decimal("0.01"):
        places = Decimal("0.0001")
    else:
        places = Decimal("0.01")
    rounded = difference.quantize(places, rounding=ROUND_HALF_UP)
    text = f"{rounded:+,.4f}".rstrip("0").rstrip(".")
    if rounded == 0:
        text = "0"
    return text, difference_unit


def humanize(value: str) -> str:
    return value.replace("_", " ").strip().capitalize()


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_metric(metric: Metric) -> str:
    row = metric.observation.row
    display_value, suffix = format_value(metric.observation.value, row["unit"])
    raw_disaggregation = json.dumps(
        metric.observation.disaggregation, sort_keys=True, separators=(",", ":")
    )
    comparison_html = ""
    if metric.comparison is not None:
        comparison = metric.comparison
        un_display, un_suffix = format_value(
            comparison.value, comparison.row["unit"]
        )
        difference = format_difference(metric)
        difference_html = ""
        if difference is not None:
            difference_value, difference_unit = difference
            difference_html = f"""
                  <p class="comparison-difference"
                     data-us-minus-un="{escape(metric.observation.value - comparison.value)}">
                    U.S. − UN: <strong>{escape(difference_value)}</strong>
                    {escape(difference_unit)}
                  </p>"""
        comparison_html = f"""
              <div class="un-value-block"
                   data-comparison-component="{escape(comparison.component)}"
                   data-comparison-year="{comparison.year}"
                   data-comparison-raw-value="{escape(comparison.row['value'])}"
                   data-completeness-status="{escape(comparison.row['completeness_status'])}">
                <div>
                  <p class="un-value-label">UN reported value</p>
                  <p class="un-value">
                    {escape(un_display)}<span>{escape(un_suffix)}</span>
                  </p>
                  <p class="un-context">{comparison.year} · {escape(comparison.row['geography'])}</p>
                </div>
{difference_html}
              </div>"""

    return f"""
            <section class="metric" aria-label="{escape(metric.label)}">
              <p class="metric-label">{escape(metric.label)}</p>
              <p class="metric-value" data-raw-value="{escape(row['value'])}">
                {escape(display_value)}<span class="metric-suffix">{escape(suffix)}</span>
              </p>
              <p class="metric-unit">{escape(row['unit'])}</p>
              <p class="metric-context"
                 data-year="{metric.observation.year}"
                 data-geography="{escape(row['geography'])}"
                 data-disaggregation="{escape(raw_disaggregation)}">
                {metric.observation.year} · {escape(row['geography'])}
              </p>
{comparison_html}
            </section>"""


def render_card(card: Card) -> str:
    goal_color = GOAL_COLORS[card.goal]
    goal_ink = readable_text_color(goal_color)
    metrics_html = "\n".join(render_metric(metric) for metric in card.metrics)
    metric_class = " metrics-multiple" if len(card.metrics) > 1 else ""
    status_class = (
        " status-needs-validation"
        if "not_archive_validated" in card.validation_status
        else ""
    )

    if card.source_url.startswith(("http://", "https://")):
        source_html = (
            f'<a href="{escape(card.source_url)}" target="_blank" '
            f'rel="noreferrer">{escape(card.source_organization)}</a>'
        )
    else:
        source_html = escape(card.source_organization)

    warning_html = ""
    if card.warnings:
        warning_text = " ".join(card.warnings)
        warning_html = f"""
          <details class="warning">
            <summary>Methodology / data note</summary>
            <p>{escape(warning_text)}</p>
          </details>"""

    comparison_header_html = ""
    comparison_source_html = ""
    comparison_note_html = ""
    if card.comparison_status:
        comparison_header_html = f"""
        <div class="comparison-heading">
          <span>Official UN comparison</span>
          <strong>{escape(card.comparison_label)}</strong>
        </div>"""
        if card.comparison_source_url.startswith(("http://", "https://")):
            comparison_database = (
                f'<a href="{escape(card.comparison_source_url)}" target="_blank" '
                'rel="noreferrer">UN SDG Global Database</a>'
            )
        else:
            comparison_database = "UN SDG Global Database"
        custodian_html = ""
        if card.comparison_custodians:
            custodian_html = (
                '<small>Custodian: '
                + escape(" / ".join(card.comparison_custodians))
                + "</small>"
            )
        comparison_source_html = f"""
          <p class="un-source"><span>UN</span><span>{comparison_database}{custodian_html}</span></p>"""
        if card.comparison_notes:
            comparison_note_html = f"""
          <div class="comparison-note">
            <strong>Comparison note</strong>
            {' '.join(f'<p>{escape(note)}</p>' for note in card.comparison_notes)}
          </div>"""

    return f"""
      <article class="indicator-card" data-indicator-id="{escape(card.indicator_id)}"
               data-sdg-goal="{card.goal}" data-goal-color="{goal_color}"
               data-comparison-status="{escape(card.comparison_status)}"
               style="--goal-color: {goal_color}; --goal-ink: {goal_ink}">
        <header class="card-header">
          <div class="goal-mark" aria-label="Sustainable Development Goal {card.goal}">
            <span>Goal</span>
            <strong>{card.goal}</strong>
          </div>
          <div class="indicator-label">
            <span>Indicator</span>
            <strong>{escape(card.indicator_id)}</strong>
          </div>
          <span class="status-badge{status_class}">{escape(humanize(card.validation_status))}</span>
        </header>

        <h2>{escape(card.title)}</h2>

{comparison_header_html}

        <p class="us-estimate-label">U.S. public-data estimate</p>

        <div class="metrics{metric_class}">
{metrics_html}
        </div>

        <footer class="card-footer">
          <p><span>Source</span>{source_html}</p>
          <p><span>Status</span>{escape(humanize(card.validation_status))}</p>
{comparison_source_html}
{comparison_note_html}
{warning_html}
        </footer>
      </article>"""


def render_page(cards: Sequence[Card]) -> str:
    cards_html = "\n".join(render_card(card) for card in cards)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>U.S. Sustainable Development Goal Indicators</title>
  <style>
    :root {{
      --page: #f4f6f8;
      --surface: #ffffff;
      --ink: #15212b;
      --muted: #5b6872;
      --line: #dce2e7;
      --soft: #eef2f5;
      --status: #1f6b4f;
      --status-bg: #e8f4ee;
      --review: #7a5211;
      --review-bg: #fff3d6;
      --un-ink: #244f70;
      --un-soft: #edf4f8;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--page);
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      background:
        radial-gradient(circle at top right, rgba(25, 72, 106, 0.09), transparent 31rem),
        var(--page);
    }}

    a {{ color: inherit; text-underline-offset: 0.16em; }}
    a:hover {{ color: var(--goal-color); }}
    a:focus-visible, summary:focus-visible {{ outline: 3px solid #1769aa; outline-offset: 3px; }}

    .page-shell {{
      width: min(1240px, calc(100% - 40px));
      margin: 0 auto;
      padding: 64px 0 72px;
    }}

    .page-header {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: end;
      gap: 32px;
      margin-bottom: 34px;
      padding-bottom: 26px;
      border-bottom: 1px solid var(--line);
    }}

    .eyebrow {{
      margin: 0 0 9px;
      color: #19486a;
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}

    h1 {{
      max-width: 760px;
      margin: 0;
      font-size: clamp(2.1rem, 5vw, 4.2rem);
      font-weight: 650;
      letter-spacing: -0.045em;
      line-height: 0.98;
    }}

    .intro {{
      max-width: 580px;
      margin: 16px 0 0;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.65;
    }}

    .header-count {{
      min-width: 138px;
      padding: 14px 16px;
      border-left: 4px solid #19486a;
      background: rgba(255, 255, 255, 0.7);
    }}

    .header-count strong {{ display: block; font-size: 1.7rem; line-height: 1; }}
    .header-count span {{ color: var(--muted); font-size: 0.82rem; }}

    .card-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 22px;
    }}

    .indicator-card {{
      position: relative;
      display: flex;
      min-width: 0;
      min-height: 620px;
      flex-direction: column;
      overflow: hidden;
      border: 1px solid var(--line);
      border-top: 6px solid var(--goal-color);
      border-radius: 14px;
      background: var(--surface);
      box-shadow: 0 9px 24px rgba(24, 38, 49, 0.06);
    }}

    .card-header {{
      display: flex;
      align-items: center;
      gap: 13px;
      padding: 21px 24px 0;
    }}

    .goal-mark {{
      display: grid;
      width: 56px;
      height: 56px;
      flex: 0 0 auto;
      place-content: center;
      border-radius: 10px;
      background: var(--goal-color);
      color: var(--goal-ink);
      text-align: center;
    }}

    .goal-mark span {{ font-size: 0.56rem; font-weight: 700; letter-spacing: 0.12em; }}
    .goal-mark strong {{ font-size: 1.35rem; line-height: 1.05; }}

    .indicator-label {{ display: grid; gap: 1px; }}
    .indicator-label span {{ color: var(--muted); font-size: 0.72rem; text-transform: uppercase; }}
    .indicator-label strong {{ font-size: 1.08rem; }}

    .status-badge {{
      margin-left: auto;
      padding: 6px 9px;
      border-radius: 999px;
      background: var(--status-bg);
      color: var(--status);
      font-size: 0.7rem;
      font-weight: 700;
      line-height: 1.2;
      text-align: center;
    }}

    .status-needs-validation {{ background: var(--review-bg); color: var(--review); }}

    h2 {{
      min-height: 3.9em;
      margin: 22px 24px 12px;
      font-size: 1rem;
      font-weight: 600;
      line-height: 1.35;
    }}

    .comparison-heading {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 0 24px;
      padding: 9px 11px;
      border-radius: 8px;
      background: var(--un-soft);
      color: var(--un-ink);
      font-size: 0.7rem;
    }}

    .comparison-heading span {{
      font-weight: 650;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}

    .comparison-heading strong {{ text-align: right; }}

    .us-estimate-label {{
      margin: 17px 24px 0;
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .metrics {{
      display: grid;
      flex: 1;
      align-content: start;
      padding: 13px 24px 22px;
    }}

    .metrics-multiple {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; }}
    .metrics-multiple .metric:nth-child(even) {{
      border-left: 1px solid var(--line);
      padding-left: 22px;
    }}
    .metrics-multiple .metric:nth-child(n+3) {{
      padding-top: 22px;
      border-top: 1px solid var(--line);
    }}

    .metric-label {{
      min-height: 1.3em;
      margin: 0 0 6px;
      color: var(--goal-color);
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}

    .metric-value {{
      margin: 0;
      font-size: clamp(2.35rem, 5vw, 4.05rem);
      font-variant-numeric: tabular-nums;
      font-weight: 650;
      letter-spacing: -0.055em;
      line-height: 1;
    }}

    .metrics-multiple .metric-value {{ font-size: clamp(1.75rem, 3.4vw, 2.65rem); }}
    .metric-suffix {{ margin-left: 0.05em; font-size: 0.48em; letter-spacing: -0.01em; }}

    .metric-unit {{
      min-height: 1.3em;
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.35;
    }}

    .metric-context {{
      margin: 12px 0 0;
      font-size: 0.86rem;
      font-weight: 650;
    }}

    .un-value-block {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: end;
      gap: 12px;
      margin-top: 16px;
      padding: 12px;
      border: 1px solid #d6e4ed;
      border-radius: 9px;
      background: #f7fafc;
    }}

    .un-value-label {{
      margin: 0 0 3px;
      color: var(--un-ink);
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.045em;
      text-transform: uppercase;
    }}

    .un-value {{
      margin: 0;
      color: #173d59;
      font-size: 1.4rem;
      font-variant-numeric: tabular-nums;
      font-weight: 700;
      line-height: 1;
    }}

    .un-value > span {{ margin-left: 0.05em; font-size: 0.58em; }}
    .un-context {{ margin: 5px 0 0; color: var(--muted); font-size: 0.68rem; }}

    .comparison-difference {{
      margin: 0;
      color: var(--muted);
      font-size: 0.68rem;
      line-height: 1.3;
      text-align: right;
    }}

    .comparison-difference strong {{
      display: block;
      color: var(--ink);
      font-size: 0.86rem;
      font-variant-numeric: tabular-nums;
    }}

    .metrics-multiple .un-value-block {{
      grid-template-columns: 1fr;
      align-items: start;
    }}

    .metrics-multiple .comparison-difference {{ text-align: left; }}

    .card-footer {{
      margin-top: auto;
      padding: 18px 24px 22px;
      border-top: 1px solid var(--line);
      background: #fafbfc;
      font-size: 0.77rem;
      line-height: 1.45;
    }}

    .card-footer > p {{ display: grid; grid-template-columns: 55px 1fr; gap: 8px; margin: 0; }}
    .card-footer > p + p {{ margin-top: 7px; }}
    .card-footer > p > span {{ color: var(--muted); }}
    .card-footer a {{ overflow-wrap: anywhere; }}

    .un-source small {{
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 0.68rem;
    }}

    .comparison-note {{
      margin-top: 13px;
      padding-top: 11px;
      border-top: 1px solid var(--line);
      color: var(--muted);
    }}

    .comparison-note strong {{ color: #46535d; }}
    .comparison-note p {{ margin: 5px 0 0; }}

    .warning {{
      margin-top: 13px;
      padding-top: 11px;
      border-top: 1px solid var(--line);
      color: var(--muted);
    }}

    .warning summary {{
      cursor: pointer;
      color: #46535d;
      font-weight: 650;
    }}

    .warning p {{ margin: 8px 0 0; }}

    .page-footer {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      margin-top: 28px;
      color: var(--muted);
      font-size: 0.78rem;
    }}

    @media (max-width: 790px) {{
      .page-shell {{ width: min(100% - 28px, 620px); padding-top: 40px; }}
      .page-header {{ grid-template-columns: 1fr; }}
      .header-count {{ width: fit-content; }}
      .card-grid {{ grid-template-columns: 1fr; }}
      .indicator-card {{ min-height: 0; }}
      h2 {{ min-height: 0; }}
    }}

    @media (max-width: 470px) {{
      .page-shell {{ width: min(100% - 20px, 440px); }}
      .card-header {{ align-items: flex-start; padding-right: 18px; }}
      .status-badge {{ max-width: 115px; }}
      .metrics-multiple {{ grid-template-columns: 1fr; }}
      .metrics-multiple .metric + .metric {{
        margin-top: 18px;
        padding-top: 18px;
        padding-left: 0;
        border-top: 1px solid var(--line);
        border-left: 0;
      }}
      .metrics-multiple .metric:nth-child(even) {{ border-left: 0; }}
      .metric-value, .metrics-multiple .metric-value {{ font-size: 2.35rem; }}
      .comparison-heading {{ align-items: flex-start; flex-direction: column; }}
      .comparison-heading strong {{ text-align: left; }}
      .un-value-block {{ grid-template-columns: 1fr; align-items: start; }}
      .comparison-difference {{ text-align: left; }}
      .page-footer {{ flex-direction: column; gap: 6px; }}
    }}

    @media print {{
      body {{ background: #fff; }}
      .page-shell {{ width: 100%; padding: 20px; }}
      .indicator-card {{ break-inside: avoid; box-shadow: none; }}
    }}
  </style>
</head>
<body>
  <main class="page-shell">
    <header class="page-header">
      <div>
        <p class="eyebrow">United States · Latest automated observations</p>
        <h1>U.S. Sustainable Development Goal Indicators</h1>
        <p class="intro">U.S. estimates calculated from publicly accessible data,
          with official UN comparisons where available. This preview shows reviewed
          comparison measures alongside the latest automated U.S. observations.</p>
      </div>
      <div class="header-count" aria-label="{len(cards)} automated indicators shown">
        <strong>{len(cards)}</strong>
        <span>automated indicators</span>
      </div>
    </header>

    <section class="card-grid" aria-label="SDG indicator cards">
{cards_html}
    </section>

    <footer class="page-footer">
      <span>Generated from standardized U.S. and reviewed UN comparison outputs</span>
      <span>Latest preferred observation per matched series</span>
    </footer>
  </main>
</body>
</html>
"""


def write_atomically(content: str) -> None:
    """Replace the output only after a complete temporary file is written."""

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=OUTPUT_PATH.parent,
            prefix=f".{OUTPUT_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, OUTPUT_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def print_summary(cards: Sequence[Card]) -> None:
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Indicator cards: {len(cards)}")
    for card in cards:
        rendered_metrics = []
        for metric in card.metrics:
            display, suffix = format_value(
                metric.observation.value, metric.observation.row["unit"]
            )
            rendered_metrics.append(
                f"{metric.label}: U.S. {display}{suffix} ({metric.observation.year})"
            )
            if metric.comparison is not None:
                un_display, un_suffix = format_value(
                    metric.comparison.value, metric.comparison.row["unit"]
                )
                rendered_metrics[-1] += (
                    f", UN {un_display}{un_suffix} ({metric.comparison.year})"
                )
        print(f"  {card.indicator_id}: " + "; ".join(rendered_metrics))


def main() -> None:
    cards = []
    for indicator_id in INDICATOR_ORDER:
        observations = read_observations(indicator_id, INPUT_FILES[indicator_id])
        comparisons = read_comparison_observations(
            indicator_id, COMPARISON_FILES[indicator_id]
        )
        cards.append(build_card(indicator_id, observations, comparisons))
    write_atomically(render_page(cards))
    print_summary(cards)


if __name__ == "__main__":
    main()
