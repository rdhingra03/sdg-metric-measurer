"""Offline tests for the three OECD donor-finance indicator transformations."""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.indicators import oecd_donor_finance as indicator
from sdg_pipeline.sources.oecd import OecdResult, SdmxObservation
from sdg_pipeline.standardized import CURRENT_METHODOLOGY_VERIFIED


def observation(year: int, value: str, **dimensions: str) -> SdmxObservation:
    """Build one small parsed OECD observation."""

    return SdmxObservation(year, Decimal(value), dimensions)


def source(dataset: str = "OECD fixture", url: str = "https://example.invalid/oecd") -> OecdResult:
    """Build controlled provenance for standardized-output tests."""

    return OecdResult(
        observations=(),
        source_organization="Organisation for Economic Co-operation and Development (OECD)",
        source_dataset=dataset,
        source_url=url,
        retrieval_method="api",
        retrieval_date="2026-08-12",
        dataflow_id="fixture",
        dataflow_version="1.0",
    )


def aid_observations() -> list[SdmxObservation]:
    """Return all nine sectors for both 8.a.1 flows in one year."""

    rows = []
    for flow, multiplier in (("C", 1), ("D", 10)):
        for position, sector in enumerate(indicator.AID_FOR_TRADE_SECTORS, start=1):
            rows.append(
                observation(2022, str(position * multiplier), FLOW_TYPE=flow, SECTOR=sector)
            )
    return rows


def dac1_observations(gni: str = "1000") -> list[SdmxObservation]:
    """Return one year of shared DAC1 measures."""

    return [
        observation(2022, gni, MEASURE="1"),
        observation(2022, "250", MEASURE="5"),
        observation(2022, "20", MEASURE="1010"),
    ]


def dac2a_observations() -> list[SdmxObservation]:
    """Return bilateral and imputed-multilateral LDC inputs."""

    return [
        observation(2022, "2", MEASURE="106"),
        observation(2022, "5", MEASURE="206"),
    ]


class AidForTradeTests(unittest.TestCase):
    """Protect sector aggregation, flow separation, and current-price warning."""

    def test_required_sectors_are_aggregated_separately_by_flow(self) -> None:
        result = indicator.calculate_aid_for_trade(
            aid_observations(), {"C": (2022,), "D": (2022,)}
        )
        by_flow = {item.flow: item.value for item in result.totals}

        self.assertEqual(Decimal("45"), by_flow["C"])
        self.assertEqual(Decimal("450"), by_flow["D"])
        self.assertEqual(18, len(result.components))

    def test_absent_sector_is_auditable_zero(self) -> None:
        rows = [
            observation(2022, "5", FLOW_TYPE="D", SECTOR="210"),
        ]
        result = indicator.calculate_aid_for_trade(rows, {"D": (2022,)})

        self.assertEqual(Decimal("5"), result.totals[0].value)
        self.assertEqual(8, len(result.warnings))
        absent = [item for item in result.components if item.sector == "220"][0]
        self.assertFalse(absent.source_observation_present)
        self.assertEqual(Decimal(0), absent.value)

    def test_standardized_rows_keep_flows_separate_and_constant(self) -> None:
        result = indicator.calculate_aid_for_trade(
            aid_observations(), {"C": (2022,), "D": (2022,)}
        )
        rows = indicator.build_aid_for_trade_standardized(result.totals, source())

        self.assertEqual(
            [{"flow": "Commitments"}, {"flow": "Disbursements"}],
            [row.disaggregation for row in rows],
        )
        self.assertEqual(
            ["million constant 2024 USD"] * 2, [row.unit for row in rows]
        )
        self.assertTrue(all("current USD" in row.data_warning for row in rows))
        self.assertTrue(
            all(row.validation_status == CURRENT_METHODOLOGY_VERIFIED for row in rows)
        )

    def test_archive_comparison_does_not_require_equality(self) -> None:
        calculated = [indicator.AidForTradeTotal(2022, "D", Decimal("1905.25"))]
        compared = indicator.compare_aid_for_trade_archive(
            calculated, {(2022, "D"): Decimal("1792.76")}
        )

        self.assertEqual(Decimal("112.49"), compared[0].difference)


class ResourceFlowsTests(unittest.TestCase):
    """Protect DAC1 measure selection, placeholder handling, and output."""

    def test_measure_five_is_selected_without_a_denominator(self) -> None:
        calculated = indicator.calculate_resource_flows(dac1_observations(), (2022,))

        self.assertEqual(Decimal("250"), calculated[0].value)

    def test_exact_legacy_zero_is_classified_as_placeholder(self) -> None:
        parsed = indicator.parse_placeholder_archive(
            "Year,Value\n2015,0\n", "10.b.1"
        )

        self.assertTrue(parsed.is_placeholder)
        self.assertEqual(2015, parsed.year)
        with self.assertRaisesRegex(RuntimeError, "not the expected"):
            indicator.parse_placeholder_archive(
                "Year,Value\n2015,1\n", "10.b.1"
            )

    def test_standardized_output_identifies_current_donor_method(self) -> None:
        calculated = indicator.calculate_resource_flows(dac1_observations(), (2022,))
        row = indicator.build_resource_flows_standardized(calculated, source())[0]

        self.assertEqual({}, row.disaggregation)
        self.assertEqual("million current USD", row.unit)
        self.assertEqual(indicator.RESOURCE_FLOWS_METHODOLOGY, row.methodology_variant)
        self.assertIn("placeholder", row.data_warning)


class OdaGniTests(unittest.TestCase):
    """Protect both 17.2.1 ratios and their interpretation."""

    def test_total_and_ldc_percentages_include_imputed_multilateral_oda(self) -> None:
        calculated = indicator.calculate_oda_gni(
            dac1_observations(), dac2a_observations(), (2022,)
        )[0]

        self.assertEqual(Decimal("2"), calculated.total_percent)
        self.assertEqual(Decimal("0.7"), calculated.ldc_percent)
        self.assertNotEqual(Decimal("0.5"), calculated.ldc_percent)

    def test_division_by_zero_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-positive GNI"):
            indicator.calculate_oda_gni(
                dac1_observations(gni="0"), dac2a_observations(), (2022,)
            )

    def test_grant_equivalent_is_audit_only_not_substituted(self) -> None:
        calculated = indicator.calculate_oda_gni(
            dac1_observations(),
            dac2a_observations(),
            (2022,),
            [observation(2022, "22", MEASURE="11010")],
        )[0]

        self.assertEqual(Decimal("2"), calculated.total_percent)
        self.assertEqual(Decimal("22"), calculated.grant_equivalent_oda)
        self.assertEqual(Decimal("2.2"), calculated.grant_equivalent_percent)

    def test_standardized_components_are_clearly_labelled_with_provenance(self) -> None:
        calculated = indicator.calculate_oda_gni(
            dac1_observations(), dac2a_observations(), (2022,)
        )
        rows = indicator.build_oda_gni_standardized(
            calculated,
            source("DAC1", "https://example.invalid/dac1"),
            source("DAC2A", "https://example.invalid/dac2a"),
        )

        self.assertEqual(
            [
                {"component": "Total ODA"},
                {"component": "Least developed countries"},
            ],
            [row.disaggregation for row in rows],
        )
        self.assertEqual("https://example.invalid/dac1", rows[0].source_url)
        self.assertIn("https://example.invalid/dac2a", rows[1].source_url)
        self.assertEqual(["2", "0.7"], [row.value for row in rows])
        self.assertTrue(all("grant-equivalent" in row.data_warning for row in rows))


if __name__ == "__main__":
    unittest.main()
