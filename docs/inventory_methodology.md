# Indicator inventory methodology

## What this deliverable does

`scripts/build_indicator_inventory.py` creates a compact inventory of the SDG indicators in the archived U.S. Open SDG project. The result is `metadata/indicator_inventory.csv`, with one row for each unique indicator.

The script does not unpack the legacy repository onto the computer. It opens `source_materials/SDGs.tar`, reads the nested ZIP file into memory, and reads the metadata, configuration, and CSV data directly from there. The only file it generates is the inventory CSV.

The inventory is reproducible: running the same script against the same source archive produces the same ordered rows and columns.

## Canonical source

The canonical source is:

- Outer archive: `source_materials/SDGs.tar`
- Repository snapshot inside it: `SDGs/sdg-master.zip`
- Repository folder inside the ZIP: `sdg-master/`

The script does not use indicator records from `sdg-develop`, `sdg-gh-pages`, `sdg-indicators-usa-main`, or `indicator-meta` to decide which indicators exist or to supply their data, status, or descriptive metadata. This prevents development or generated website copies from creating duplicate indicators.

Within `sdg-master`, the script uses three source areas:

- `meta/` for descriptive metadata, organizations, sources, coverage, and methods
- `indicator-config/` for reporting status and display-oriented titles
- `data/indicator_<indicator ID>.csv` for observations and year coverage

The `sdg-master` configuration references the standard `sdg-translations` package, but the global English title catalog itself is not bundled in that ZIP. For the small number of configuration titles that remain internal tokens, the script uses the already-resolved English titles in `SDGs/sdg-gh-pages.zip` from the same outer archive. This is strictly a translation lookup: `sdg-master` remains the canonical source for the indicator universe and every other inventory field.

## Output columns

- **indicator_id** — The complete SDG indicator number in familiar dotted form, such as `1.2.1`.
- **sdg_goal** — The first part of the indicator number, such as goal `1`.
- **sdg_target** — The goal and target together, such as `1.2` or `10.a`.
- **indicator_title** — The most useful readable title found in the metadata or indicator configuration, with an archived English translation lookup used when the source contains only an internal token.
- **reporting_status** — The source project's status, normally `complete` or `notstarted`. It is left blank if the source does not specify a status.
- **data_file_exists** — `true` when the canonical source contains the normally named CSV file for the indicator; otherwise `false`.
- **data_quality** — A reproducible classification of the canonical data file: `populated`, `single_observation`, `placeholder`, or `missing`.
- **observation_count** — The number of CSV rows whose `Value` cell is populated. Zero is a valid value, so a row containing `0` is counted.
- **earliest_year** — The earliest four-digit year among rows with a populated value.
- **latest_year** — The latest four-digit year among rows with a populated value.
- **source_organization** — The first available responsible organization or data-producing organization in the metadata.
- **source_dataset** — The first available survey, dataset, source title, or source-type description.
- **source_url** — Source web addresses selected using the source-field rules below. Multiple addresses are separated by ` | `.
- **source_url_origin** — Where the selected URL was found: `explicit_source_url`, `source_dataset_field`, `source_notes`, `reference_field`, or `missing`.
- **geographic_coverage** — The national or disaggregation coverage stated in the source.
- **computation_method** — The first available national, general, or `DATA_COMP` computation description.
- **inventory_warnings** — A normally empty field that records a source conflict or nonstandard alternate file requiring later human review. Multiple warnings are separated by ` | `.

## Data-quality categories

Every indicator receives exactly one `data_quality` value:

- **populated** — The canonical data file contains multiple populated observations and is not the known placeholder.
- **single_observation** — The canonical data file contains exactly one populated observation and is not the known placeholder.
- **placeholder** — After normalizing line endings, the entire canonical CSV is exactly `Year,Value` followed by `2015,0`.
- **missing** — No normally named canonical data file exists for the indicator.

The original `observation_count`, `earliest_year`, and `latest_year` values are retained even for placeholders so the inventory describes the source faithfully. The explicit quality label prevents the placeholder row from being mistaken for substantive data.

## Parsing and precedence assumptions

The indicator universe is the union of normally named indicator files in the canonical `meta`, `indicator-config`, and `data` folders. This means an indicator is retained even when one of those three components is missing.

Canonical filenames use hyphens, such as `10-1-1`. A stray alternate file named `data/indicator_10_1_1.csv` also exists, but the legacy build pattern accepts hyphenated filenames and a standard `data/indicator_10-1-1.csv` is already present. The script follows the build convention and ignores the underscore-named alternate so indicator 10.1.1 is not counted twice.

Five indicators have both Markdown and YAML metadata files. In those cases, the script starts with the Markdown record used by Open SDG and fills missing fields from the same-ID YAML record. Existing non-empty values are not overwritten.

Reporting status is taken from `indicator-config` first, then from metadata if necessary. If both values exist and conflict, configuration still wins and the disagreement is recorded in `inventory_warnings` rather than silently resolved.

Titles prefer readable metadata or configuration text. When every available title is an internal token such as `global_indicators.2-2-3-title`, the script looks up the corresponding readable English title in the archived English `sdg-gh-pages` metadata. It does not manually hard-code titles or use that generated repository to add indicators.

Source URLs use this precedence:

1. Numbered `source_url_*` fields, recorded as `explicit_source_url`.
2. Clearly source-related dataset fields such as `SOURCE_TYPE`, `DATA_SOURCE`, `source_agency_survey_dataset_*`, and `source_title_*`, recorded as `source_dataset_field`.
3. `source_notes_*`, recorded as `source_notes`.
4. `international_and_national_references`, recorded as `reference_field`.
5. If none contain an HTTP or HTTPS address, the URL is blank and the origin is `missing`.

The search stops at the first category containing URLs and preserves all unique URLs from that category using ` | `. It does not search definitions, computation text, general goal links, or arbitrary unrelated fields.

The script includes a small, dependency-free reader for the top-level YAML fields used here. It understands simple scalars, wrapped text, double-quoted escape sequences, and the backslash line-continuation style present in the archive. Literal tab and continuation notation is cleaned before values enter the CSV. The reader is not intended to be a general YAML implementation, and nested website display settings are deliberately ignored because they are not inventory fields.

Noncanonical filenames that normalize to an existing indicator ID are not substituted automatically. They are recorded in `inventory_warnings`. This includes the alternate data file for 10.1.1 and alternate metadata for 15.a.1 and 15.b.1. These warnings preserve the canonical filename and reporting-status precedence while making legacy conflicts visible for later review.

## Limitations

- `data_file_exists` means a canonical file is present. It does not guarantee that the figures have been independently verified.
- The exact `2015,0` stub is classified as a placeholder. Other unusual values, including binary series, are preserved and are not judged automatically.
- Observation counts include every populated `Value` row, including separate demographic or other disaggregations for the same year.
- The year range only uses exact four-digit values from the `Year` column. Other time formats would be ignored rather than guessed.
- Empty metadata stays empty in the inventory. Apart from the narrowly documented English title lookup, the script does not infer organizations, datasets, URLs, coverage, or methods from outside `sdg-master`.
- Some source fields overlap in meaning. The documented precedence rules select one value for the compact inventory; they do not discard or rewrite the original archive metadata.
- A source URL found in a dataset field does not guarantee that the field also contains a clean human-readable dataset name.
