# Source research queue methodology

## Purpose

`scripts/build_source_research_queue.py` creates `metadata/source_research_queue.csv` from the canonical indicator inventory. The queue contains only indicators whose current `source_url` is blank.

The script does not research sources or make judgments about them. It copies useful context from `metadata/indicator_inventory.csv`, sets `research_status` to `not_started`, and leaves every other research field blank for later verified work.

Running the script again against the same inventory produces the same initial queue and ordering. Because regeneration resets the research columns, a queue containing human research should be preserved or merged deliberately before rebuilding it.

## Queue ordering

Rows are ordered so the most immediately useful provenance work appears first:

1. `populated` indicators
2. `single_observation` indicators
3. `placeholder` indicators
4. `missing`-data indicators

Within each group, rows are sorted by SDG goal and indicator ID. This keeps related indicators close together and makes batch research easier.

## Research fields

Only `research_status` is populated initially, with `not_started`. The intended future values are:

- **research_status:** `not_started`, `researching`, `source_identified`, `needs_review`, or `not_applicable`
- **us_applicability:** `applicable`, `not_applicable`, or `unclear`
- **source_type:** `us_federal`, `us_state_local`, `international_official`, `other_public`, or `unclear`
- **retrieval_method:** `api`, `direct_download`, `webpage_table`, `manual`, or `unknown`
- **automation_feasibility:** `high`, `medium`, `low`, or `not_applicable`
- **confidence:** `high`, `medium`, or `low`

The proposed organization, dataset, URL, notes, and verification date remain free-text fields. A practical `date_verified` format is `YYYY-MM-DD`.

## Source-selection principles

An international SDG custodian is not automatically the preferred source for a U.S. estimate. Custodian agencies define or coordinate global reporting, but a U.S. federal statistical agency may publish the more authoritative underlying U.S. observations.

Where appropriate, this project prefers authoritative U.S. public data that supports transparent, first-principles estimates. Examples include official APIs, downloadable tables, and documented statistical releases from federal agencies. The selected source must still match the SDG concept closely enough to be defensible.

Some indicators may legitimately require an international official source. This can happen when the indicator measures international finance, treaty reporting, comparisons between countries, or a series maintained centrally by a recognized custodian.

Some SDG indicators may not be applicable to the United States, or may not have a defensible U.S. equivalent. Researchers should use `us_applicability` and explain the decision in `research_notes` rather than forcing a weak substitute.

Repeated or closely related SDG indicators may share one underlying data pipeline. Researchers should look for opportunities to reuse one API client, download process, transformation, or verification method across several indicators while keeping indicator-specific definitions visible.

## Manual verification requirement

Every proposed source must be manually verified before it is treated as canonical. At minimum, verification should confirm:

- the publisher is authoritative for the data;
- the dataset measures the intended indicator or a clearly documented proxy;
- the geographic coverage is appropriate for the United States;
- units, population, time period, and disaggregations are understood;
- the proposed URL is stable enough to retrieve or document the source;
- the retrieval method and automation assessment are realistic; and
- any caveats are recorded in `research_notes`.

Finding a plausible web page is not enough. A row should move to `source_identified` only when the proposed organization, dataset, and URL have been checked. Ambiguous cases should use `needs_review` and remain noncanonical.
