# OECD donor-finance batch: SDG 8.a.1, 10.b.1, and 17.2.1

## Why these indicators run together

These three indicators describe the United States as a provider of development
finance. They all use official OECD statistics and the project's existing OECD
SDMX connector. Running them as one batch avoids repeating network, provenance,
archive, validation, and output-writing code.

The batch deliberately does **not** implement 2.a.2, 3.b.2, 4.b.1, or 9.a.1.
Those four current UN indicators apply only to countries on the OECD DAC List
of ODA Recipients. Their legacy U.S. files are nonstandard United-States-as-
donor adaptations and should be retained only as historical context.

## SDG 8.a.1: Aid for Trade

SDG 8.a.1 measures Aid-for-Trade commitments and gross disbursements. The
pipeline retrieves OECD Creditor Reporting System dataflow
`OECD.DCD.FSD,DSD_CRS@DF_CRS`, version `1.6`, with:

| Selection | OECD code |
|---|---|
| Donor | `USA` |
| Recipient grouping | `DPGC` (developing countries) |
| Sectors | `210`, `220`, `230`, `240`, `250`, `310`, `320`, `331`, `332` |
| Measure | `100` (ODA) |
| Flows | `C` commitments and `D` disbursements |
| Price | `Q` (constant prices) |
| Base period | 2024 |
| Unit | USD millions |

The nine sectors are added separately for each year and each flow. Commitments
and disbursements are never mixed. The standardized rows use the
disaggregations `{"flow":"Commitments"}` and
`{"flow":"Disbursements"}`.

The legacy archive uses current dollars. Its values are compared only as a
methodology-break diagnostic; equality with the canonical constant-2024-dollar
series is neither expected nor required.

## SDG 10.b.1: total resource flows

SDG 10.b.1 uses OECD DAC1 dataflow
`OECD.DCD.FSD,DSD_DAC1@DF_DAC1`, version `1.7`:

| Selection | OECD code |
|---|---|
| Donor | `USA` |
| Sector | `_Z` (not applicable/total) |
| Measure | `5` (official and private flows) |
| Flow | `1140` (net disbursements) |
| Price | `V` (current prices) |
| Unit | USD millions |

This is a direct annual value; it has no denominator. The archived `2015,0`
record is the canonical legacy placeholder pattern, not an observation, so it
is excluded from validation.

## SDG 17.2.1: net ODA as a percentage of GNI

The standardized file contains two separately labelled components.

### Total ODA

The batch retrieves DAC1 measure `1010` (net ODA) and measure `1` (GNI), both
using net-disbursement flow `1140` and current prices, then calculates:

```text
100 * net ODA / GNI
```

### Least developed countries

OECD DAC2A dataflow `OECD.DCD.FSD,DSD_DAC2@DF_DAC2A`, version `1.6`, supplies:

- measure `206`: bilateral/net ODA disbursements to LDCs;
- measure `106`: imputed multilateral ODA to LDCs.

The calculation is:

```text
100 * (measure 206 + measure 106) / GNI
```

Including imputed multilateral ODA is required; measure 206 alone understates
the LDC component. The standardized disaggregations are
`{"component":"Total ODA"}` and
`{"component":"Least developed countries"}`.

### Net flows versus grant equivalents

The formal indicator calculation uses net ODA flows. OECD also publishes
headline ODA on a grant-equivalent basis from 2018 onward. The batch retrieves
DAC1 measure `11010` (ODA grant equivalent), divides it by the same GNI, and
records both the amount and percentage in the audit file. It never substitutes
this audit comparison for the canonical net-flow calculation.

The legacy `2015,0` is a placeholder and is not validation data.

## Standardized and audit outputs

The batch writes:

- `data_processed/standardized/sdg_8_a_1.csv`
- `data_processed/standardized/sdg_10_b_1.csv`
- `data_processed/standardized/sdg_17_2_1.csv`
- `data_processed/audit/sdg_8_a_1_inputs.csv`
- `data_processed/audit/sdg_10_b_1_inputs.csv`
- `data_processed/audit/sdg_17_2_1_inputs.csv`

The standardized files use the common project schema and validation status
`current_methodology_verified`. The audit files retain source components that
do not belong in the common schema, including the nine Aid-for-Trade sectors,
the direct DAC1 resource-flow measure, and every 17.2.1 numerator and
denominator.

All six files are fully prepared as temporary files before any prior successful
output is replaced. A required OECD retrieval, source validation, calculation,
or output-preparation failure therefore leaves existing outputs intact.

## Optional UN comparison

Official UN SDG series are retrieved as non-blocking cross-checks. OECD remains
canonical. Identical duplicate UN observations are collapsed safely,
conflicting duplicates are rejected, and a UN outage does not fail an otherwise
successful OECD run. Recent OECD/UN differences may reflect release timing and
revisions and are reported without changing OECD values.

## How to run the batch

From the project root:

```bash
python3 scripts/fetch_oecd_donor_finance_batch.py
```

The command requires internet access to the official OECD service. It reports
the retrieved ranges and latest values, optional UN comparisons, legacy archive
diagnostics, and every output path.
