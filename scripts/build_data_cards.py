#!/usr/bin/env python3
"""Build a self-contained HTML preview from standardized SDG outputs.

The generated page contains no manually entered observations. Every displayed
year, value, title, unit, source, status, and warning is read from the existing
CSV files in ``data_processed/standardized``. Because the data are embedded at
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
OUTPUT_PATH = PROJECT_ROOT / "output" / "data_cards" / "index.html"

INDICATOR_ORDER = (
    "4.2.2",
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

# Only presentation rules live here. The observations themselves always come
# from the standardized CSVs. The colors follow the familiar SDG goal palette.
GOAL_COLORS = {
    "4": "#c5192d",
    "8": "#a21942",
    "10": "#dd1367",
    "15": "#56c02b",
    "17": "#19486a",
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


@dataclass(frozen=True)
class Observation:
    """One parsed row from a standardized indicator CSV."""

    row: Mapping[str, str]
    year: int
    value: Decimal
    disaggregation: Mapping[str, str]


@dataclass(frozen=True)
class Metric:
    """One headline value shown within an indicator card."""

    label: str
    observation: Observation


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

    # An empty disaggregation object is the national headline series. This is
    # especially important for 4.2.2, whose CSV also contains male/female rows.
    label = "National" if indicator_id == "4.2.2" else "Latest observation"
    return (Metric(label, latest_matching(observations, {})),)


def one_value(values: Sequence[str], field_name: str, indicator_id: str) -> str:
    """Require a shared descriptive field to agree across selected rows."""

    unique = {value.strip() for value in values if value.strip()}
    if len(unique) != 1:
        raise ValueError(
            f"Indicator {indicator_id} has inconsistent or missing {field_name}: "
            + repr(sorted(unique))
        )
    return unique.pop()


def build_card(indicator_id: str, observations: Sequence[Observation]) -> Card:
    metrics = select_metrics(indicator_id, observations)
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
    goal = indicator_id.split(".", 1)[0]
    return Card(
        indicator_id=indicator_id,
        goal=goal,
        title=title,
        metrics=metrics,
        source_organization=source_organization,
        source_url=first_source_url,
        validation_status=" / ".join(validation_statuses),
        warnings=warnings,
    )


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
            </section>"""


def render_card(card: Card) -> str:
    goal_color = GOAL_COLORS.get(card.goal, "#4b5563")
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

    return f"""
      <article class="indicator-card" data-indicator-id="{escape(card.indicator_id)}"
               style="--goal-color: {goal_color}">
        <header class="card-header">
          <div class="goal-mark" aria-label="Sustainable Development Goal {card.goal}">
            <span>SDG</span>
            <strong>{card.goal}</strong>
          </div>
          <div class="indicator-label">
            <span>Indicator</span>
            <strong>{escape(card.indicator_id)}</strong>
          </div>
          <span class="status-badge{status_class}">{escape(humanize(card.validation_status))}</span>
        </header>

        <h2>{escape(card.title)}</h2>

        <div class="metrics{metric_class}">
{metrics_html}
        </div>

        <footer class="card-footer">
          <p><span>Source</span>{source_html}</p>
          <p><span>Status</span>{escape(humanize(card.validation_status))}</p>
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
  <title>U.S. SDG Data Cards</title>
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
      min-height: 440px;
      flex-direction: column;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--surface);
      box-shadow: 0 9px 24px rgba(24, 38, 49, 0.06);
    }}

    .indicator-card::before {{
      position: absolute;
      inset: 0 auto 0 0;
      width: 7px;
      background: var(--goal-color);
      content: "";
    }}

    .card-header {{
      display: flex;
      align-items: center;
      gap: 13px;
      padding: 22px 24px 0 29px;
    }}

    .goal-mark {{
      display: grid;
      width: 56px;
      height: 56px;
      flex: 0 0 auto;
      place-content: center;
      border-radius: 50%;
      background: var(--goal-color);
      color: #fff;
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
      margin: 22px 28px 5px 29px;
      font-size: 1rem;
      font-weight: 600;
      line-height: 1.35;
    }}

    .metrics {{
      display: grid;
      flex: 1;
      align-content: center;
      padding: 16px 28px 22px 29px;
    }}

    .metrics-multiple {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; }}
    .metrics-multiple .metric + .metric {{ border-left: 1px solid var(--line); padding-left: 22px; }}

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

    .card-footer {{
      margin-top: auto;
      padding: 18px 28px 22px 29px;
      border-top: 1px solid var(--line);
      background: #fafbfc;
      font-size: 0.77rem;
      line-height: 1.45;
    }}

    .card-footer > p {{ display: grid; grid-template-columns: 55px 1fr; gap: 8px; margin: 0; }}
    .card-footer > p + p {{ margin-top: 7px; }}
    .card-footer > p > span {{ color: var(--muted); }}
    .card-footer a {{ overflow-wrap: anywhere; }}

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
      .metric-value, .metrics-multiple .metric-value {{ font-size: 2.35rem; }}
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
        <p class="eyebrow">United States · Automated indicators</p>
        <h1>SDG data cards</h1>
        <p class="intro">A latest-observation view of the project’s standardized,
          reproducible indicator outputs. Full historical series remain in the
          underlying CSV files.</p>
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
      <span>Generated from data_processed/standardized</span>
      <span>Latest available observation per series</span>
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
                f"{metric.label}: {display}{suffix} ({metric.observation.year})"
            )
        print(f"  {card.indicator_id}: " + "; ".join(rendered_metrics))


def main() -> None:
    cards = []
    for indicator_id in INDICATOR_ORDER:
        observations = read_observations(indicator_id, INPUT_FILES[indicator_id])
        cards.append(build_card(indicator_id, observations))
    write_atomically(render_page(cards))
    print_summary(cards)


if __name__ == "__main__":
    main()
