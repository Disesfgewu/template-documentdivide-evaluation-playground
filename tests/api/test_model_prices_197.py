"""Per-model prices (Issue #197, PM: a stronger judge must be estimated, capped and charged at its own price). No database."""
import os
import unittest
from types import SimpleNamespace as S
from unittest.mock import patch

from lab_api import run_estimate as est
from lab_api.model_prices import BUILTIN_UNVERIFIED, ModelPriceError, model_key, overrides, price_for

CONFIGURED = (1_000_000, 5_000_000)         # the owner's configured model: claude-haiku-4-5 ($1 / $5)


class PriceLookupTests(unittest.TestCase):
    def test_the_configured_model_keeps_the_configured_pair_and_a_date_suffix_is_ignored(self):
        for name in ("claude-haiku-4-5-20251001", "claude-haiku-4-5", "anything-unlisted"):
            self.assertEqual(price_for(name, CONFIGURED, {}), (*CONFIGURED, "configured"), name)
        self.assertEqual(model_key("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_the_stronger_models_have_their_own_unverified_prices(self):
        self.assertEqual(price_for("claude-sonnet-5-5", CONFIGURED, {}), (2_000_000, 10_000_000, "builtin_unverified"))
        self.assertEqual(price_for("claude-opus-5-5", CONFIGURED, {}), (4_000_000, 20_000_000, "builtin_unverified"))
        self.assertEqual(price_for("claude-haiku-5-5", CONFIGURED, {}), (100_000, 500_000, "builtin_unverified"))
        self.assertNotIn("claude-haiku-4-5", BUILTIN_UNVERIFIED)           # the configured values decide for it, as before

    def test_the_owner_override_beats_the_table_and_may_price_any_model(self):
        env = {"LAB_GEN_MODEL_PRICES": '{"claude-sonnet-5-5": [3000000, 15000000], "my-model-20260101": [1, 2]}'}
        self.assertEqual(price_for("claude-sonnet-5-5", CONFIGURED, env), (3_000_000, 15_000_000, "owner_override"))
        self.assertEqual(price_for("my-model", None, env), (1, 2, "owner_override"))

    def test_no_price_at_all_is_an_error_never_zero(self):
        with self.assertRaises(ModelPriceError):
            price_for("unknown-model", None, {})

    def test_a_bad_override_is_refused_without_echoing_it(self):
        for bad in ("not json", '{"m": [1]}', '{"m": ["a", 2]}', '{"m": [-1, 2]}', '{"m": [1, 2000000000]}', '{"m": [1.5, 2]}', "[1, 2]"):
            with self.subTest(bad=bad), self.assertRaises(ModelPriceError) as ctx:
                overrides({"LAB_GEN_MODEL_PRICES": bad})
            self.assertNotIn(bad, str(ctx.exception))
        self.assertEqual(overrides({}), {})
        self.assertEqual(overrides({"LAB_GEN_MODEL_PRICES": "  "}), {})


class EstimatePricesTests(unittest.TestCase):
    settings = S(gen_price_in_micros_per_mtok=CONFIGURED[0], gen_price_out_micros_per_mtok=CONFIGURED[1])

    def test_the_estimate_prices_follow_the_chosen_model(self):
        with patch.dict(os.environ, {"LAB_GEN_MODEL_PRICES": ""}):
            self.assertEqual(est.prices(self.settings), CONFIGURED)                                    # no model: the configured pair, unchanged
            self.assertEqual(est.prices(self.settings, "claude-haiku-4-5-20251001"), CONFIGURED)
            self.assertEqual(est.prices(self.settings, "claude-opus-5-5"), (4_000_000, 20_000_000))
            self.assertEqual(est.prices(self.settings, "claude-haiku-5-5"), (100_000, 500_000))

    def test_the_same_prompt_costs_more_with_a_stronger_model_in_the_worst_case_figure(self):
        from lab_db.generation import worst_case_micros
        with patch.dict(os.environ, {"LAB_GEN_MODEL_PRICES": ""}):
            cheap = worst_case_micros(40_000, 500, *est.prices(self.settings, "claude-haiku-4-5"))
            strong = worst_case_micros(40_000, 500, *est.prices(self.settings, "claude-opus-5-5"))
        self.assertGreater(strong, 3 * cheap)

    def test_model_info_says_where_the_price_comes_from(self):
        with patch.dict(os.environ, {"LAB_GEN_MODEL_PRICES": ""}):
            self.assertEqual(est._model_info(self.settings, None), {})
            info = est._model_info(self.settings, "claude-sonnet-5-5")
            self.assertEqual((info["price_source"], info["price_in_micros_per_mtok"], info["price_out_micros_per_mtok"]), ("builtin_unverified", 2_000_000, 10_000_000))
            self.assertEqual(est._model_info(self.settings, "claude-haiku-4-5")["price_source"], "configured")

    def test_an_unpriced_model_without_configured_prices_is_an_estimate_error(self):
        none = S(gen_price_in_micros_per_mtok=None, gen_price_out_micros_per_mtok=None)
        with patch.dict(os.environ, {"LAB_GEN_MODEL_PRICES": ""}), self.assertRaises(est.EstimateError):
            est.prices(none, "some-unlisted-model")
        self.assertEqual(est.prices(none, "claude-sonnet-5-5"), (2_000_000, 10_000_000))                # a listed model needs no configured pair


class WorkerChargesTheJobsModelTests(unittest.TestCase):
    def test_the_worker_prices_a_job_at_its_models_price(self):
        from lab_api.generate_worker import GenModelConfig, _priced_for
        cfg = GenModelConfig(protocol="anthropic", base_url="https://x", allowed_host="x", input_micros_per_mtok=CONFIGURED[0], output_micros_per_mtok=CONFIGURED[1])
        with patch.dict(os.environ, {"LAB_GEN_MODEL_PRICES": ""}):
            same = _priced_for(cfg, "claude-haiku-4-5-20251001")
            strong = _priced_for(cfg, "claude-sonnet-5-5")
            unknown = _priced_for(cfg, "fake-model")
        self.assertEqual((same.input_micros_per_mtok, same.output_micros_per_mtok), CONFIGURED)
        self.assertEqual((strong.input_micros_per_mtok, strong.output_micros_per_mtok), (2_000_000, 10_000_000))
        self.assertEqual((unknown.input_micros_per_mtok, unknown.output_micros_per_mtok), CONFIGURED)     # unlisted: the configured pair, as before
        self.assertEqual((strong.base_url, strong.allowed_host), (cfg.base_url, cfg.allowed_host))        # only the price changes
