"""Tests for examples/mql_companion/mt5_mcp_server.py.

The bridge has two jobs that must not fail quietly:

* speak the MCP JSON-RPC dialect ``openjarvis.mcp.client`` expects, so that
  ``[tools.mcp]`` can spawn it and wrap its tools like any other server;
* refuse to do anything expensive by accident — no trading tool unless it was
  asked for, no orders on a non-demo account, no position without a stop, no
  volume that is not a lot-step multiple.

Everything runs against the deterministic stub backend, so the suite needs no
MetaTrader, no Windows and no network. The one subprocess test proves the stdio
transport end to end with the framework's own client.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "examples" / "mql_companion" / "mt5_mcp_server.py"

# A pinned clock keeps the synthetic market reproducible across runs.
FIXED_NOW = 1_790_000_000.0


def _load_module() -> ModuleType:
    """Import the example script by path.

    Registering it in ``sys.modules`` *before* ``exec_module`` is required:
    the module uses ``from __future__ import annotations`` together with
    ``@dataclass``, and dataclasses resolves annotations through sys.modules.
    """
    spec = importlib.util.spec_from_file_location("mql_mt5_mcp_server", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bridge() -> ModuleType:
    return _load_module()


@pytest.fixture()
def stub(bridge: ModuleType) -> Any:
    return bridge.StubTerminal(now_fn=lambda: FIXED_NOW)


@pytest.fixture()
def server(bridge: ModuleType, stub: Any) -> Any:
    return bridge.build_server(stub)


def _bridge() -> ModuleType:
    """The module under test (loaded once by the module-scoped fixture)."""
    return sys.modules["mql_mt5_mcp_server"]


def _call(server: Any, name: str, **arguments: Any) -> Dict[str, Any]:
    """Invoke a tool through the MCP request path and decode its payload."""
    response = server.handle(
        _bridge().MCPRequest(
            method="tools/call",
            params={"name": name, "arguments": arguments},
            id=1,
        )
    )
    if response.error is not None:
        return {
            "isError": True,
            "text": f"{response.error['code']}: {response.error['message']}",
        }
    result = response.result
    text = "".join(
        block["text"] for block in result["content"] if block["type"] == "text"
    )
    return {"isError": result["isError"], "text": text}


def _payload(server: Any, name: str, **arguments: Any) -> Any:
    out = _call(server, name, **arguments)
    assert not out["isError"], out["text"]
    return json.loads(out["text"])


# ---------------------------------------------------------------------------
# The synthetic market has to be believable and reproducible
# ---------------------------------------------------------------------------


class TestStubMarket:
    def test_prices_are_deterministic_for_a_pinned_clock(
        self, bridge: ModuleType
    ) -> None:
        first = bridge.StubTerminal(now_fn=lambda: FIXED_NOW)
        second = bridge.StubTerminal(now_fn=lambda: FIXED_NOW)
        assert first.tick("EURUSD") == second.tick("EURUSD")

    def test_prices_move_when_the_bar_index_moves(self, bridge: ModuleType) -> None:
        now = bridge.StubTerminal(now_fn=lambda: FIXED_NOW).tick("EURUSD")
        later = bridge.StubTerminal(now_fn=lambda: FIXED_NOW + 3600).tick("EURUSD")
        assert now["bid"] != later["bid"]

    def test_quote_is_wide_by_the_declared_spread(self, stub: Any) -> None:
        info = stub.symbol_info("EURUSD")
        tick = stub.tick("EURUSD")
        spread = (tick["ask"] - tick["bid"]) / info["point"]
        assert round(spread) == info["spread_points"] == 12

    def test_bars_are_oldest_first_and_continuous(self, stub: Any) -> None:
        bars = stub.rates("EURUSD", "H1", 6, 0)
        assert len(bars) == 6
        epochs = [b["time_epoch"] for b in bars]
        assert epochs == sorted(epochs), "bars must be oldest first"
        for previous, current in zip(bars, bars[1:]):
            assert current["open"] == previous["close"]
        for b in bars:
            assert b["high"] >= max(b["open"], b["close"])
            assert b["low"] <= min(b["open"], b["close"])

    def test_bar_spacing_follows_the_timeframe(self, stub: Any) -> None:
        m5 = stub.rates("EURUSD", "M5", 3, 0)
        h1 = stub.rates("EURUSD", "H1", 3, 0)
        d1 = stub.rates("EURUSD", "D1", 3, 0)
        assert m5[1]["time_epoch"] - m5[0]["time_epoch"] == 300
        assert h1[1]["time_epoch"] - h1[0]["time_epoch"] == 3600
        assert d1[1]["time_epoch"] - d1[0]["time_epoch"] == 86400

    def test_shift_walks_back_in_time(self, stub: Any) -> None:
        current = stub.rates("EURUSD", "H1", 1, 0)[0]
        previous = stub.rates("EURUSD", "H1", 1, 1)[0]
        assert current["time_epoch"] - previous["time_epoch"] == 3600
        assert previous["close"] == stub.rates("EURUSD", "H1", 2, 0)[0]["close"]

    def test_unknown_timeframe_is_rejected_with_the_valid_list(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        with pytest.raises(bridge.Mt5Error) as excinfo:
            stub.rates("EURUSD", "H7", 5, 0)
        assert "H7" in str(excinfo.value)
        assert "MN1" in str(excinfo.value)

    def test_unknown_symbol_lists_the_market(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        with pytest.raises(bridge.Mt5Error) as excinfo:
            stub.tick("NOPE")
        message = str(excinfo.value)
        assert "NOPE" in message
        assert "EURUSD" in message

    def test_account_agrees_with_the_positions_it_holds(self, stub: Any) -> None:
        account = stub.account()
        positions = stub.positions(None, None)
        assert positions, "the stub should start with open positions"
        floating = sum(p["profit"] + p["swap"] + p["commission"] for p in positions)
        assert account["profit"] == pytest.approx(floating, abs=0.01)
        assert account["equity"] == pytest.approx(account["balance"] + floating)
        assert account["margin_free"] == pytest.approx(
            account["equity"] - account["margin"]
        )

    def test_trade_mode_label_matches_the_raw_constant(
        self, bridge: ModuleType
    ) -> None:
        for label, raw in (("demo", 0), ("contest", 1), ("real", 2)):
            account = bridge.StubTerminal(
                trade_mode=label, now_fn=lambda: FIXED_NOW
            ).account()
            assert account["trade_mode"] == label
            assert account["trade_mode_raw"] == raw

    def test_every_payload_is_marked_synthetic(self, stub: Any) -> None:
        assert stub.status()["synthetic"] is True
        assert stub.account()["synthetic"] is True
        assert stub.tick("EURUSD")["synthetic"] is True
        assert stub.symbol_info("EURUSD")["synthetic"] is True
        assert all(p["synthetic"] is True for p in stub.positions(None, None))
        assert all(o["synthetic"] is True for o in stub.orders(None))

    def test_symbol_filtering_by_pattern_and_magic(self, stub: Any) -> None:
        assert [s["symbol"] for s in stub.symbols("*USD", True, 10)] == [
            "EURUSD",
            "GBPUSD",
            "USDJPY",
            "XAUUSD",
            "BTCUSD",
        ]
        assert [s["symbol"] for s in stub.symbols("XA*", True, 10)] == ["XAUUSD"]
        assert len(stub.symbols("", True, 2)) == 2
        assert stub.positions("EURUSD", None)[0]["symbol"] == "EURUSD"
        assert stub.positions(None, 999) == []

    def test_symbol_info_carries_what_an_ea_must_respect(self, stub: Any) -> None:
        info = stub.symbol_info("XAUUSD")
        for key in (
            "digits",
            "point",
            "spread_points",
            "volume_min",
            "volume_max",
            "volume_step",
            "stops_level_points",
            "contract_size",
            "tick_size",
            "tick_value",
            "filling_modes",
            "expiration_modes",
            "swap_mode",
            "order_types",
        ):
            assert key in info, f"symbol_info lost {key}"
        assert info["digits"] == 2
        assert info["point"] == 0.01
        assert "buy" in info["order_types"] and "sell" in info["order_types"]
        assert info["filling_modes"]

    def test_calc_prices_a_trade_the_way_the_contract_says(self, stub: Any) -> None:
        info = stub.symbol_info("EURUSD")
        tick = stub.tick("EURUSD")
        out = stub.calc("EURUSD", "buy", 0.5, tick["ask"], tick["ask"] + 0.0010)
        expected_profit = 0.0010 * 0.5 * info["contract_size"]
        assert out["profit_at_close"] == pytest.approx(expected_profit, abs=0.01)
        assert out["margin_per_lot"] == pytest.approx(
            tick["ask"] * info["contract_size"] / 100.0, abs=0.01
        )
        assert out["price_open"] == tick["ask"]


# ---------------------------------------------------------------------------
# The tool table is what the model sees
# ---------------------------------------------------------------------------


class TestToolTable:
    def test_read_only_by_default(self, bridge: ModuleType, server: Any) -> None:
        response = server.handle(bridge.MCPRequest(method="tools/list", id=1))
        tools = response.result["tools"]
        names = [t["name"] for t in tools]
        assert names == [
            "mt5_status",
            "mt5_account",
            "mt5_symbols",
            "mt5_symbol_info",
            "mt5_tick",
            "mt5_rates",
            "mt5_positions",
            "mt5_orders",
            "mt5_calc",
            "mt5_tester_report",
            "mt5_tester_compare",
            "mt5_tester_optimization",
        ]
        assert "mt5_order_send" not in names
        assert "mt5_tester_run" not in names
        for tool in tools:
            assert tool["annotations"]["readOnlyHint"] is True
            assert tool["annotations"]["destructiveHint"] is False

    def test_the_trading_tool_needs_an_explicit_flag(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        response = server.handle(bridge.MCPRequest(method="tools/list", id=1))
        tools = {t["name"]: t for t in response.result["tools"]}
        assert "mt5_order_send" in tools
        annotations = tools["mt5_order_send"]["annotations"]
        assert annotations["readOnlyHint"] is False
        assert annotations["destructiveHint"] is True

    def test_schemas_are_well_formed(self, server: Any, bridge: ModuleType) -> None:
        response = server.handle(bridge.MCPRequest(method="tools/list", id=1))
        for tool in response.result["tools"]:
            schema = tool["inputSchema"]
            name = tool["name"]
            assert schema["type"] == "object", name
            assert schema["additionalProperties"] is False, name
            properties = schema["properties"]
            assert isinstance(properties, dict), name
            for key in schema.get("required", []):
                assert key in properties, f"{name}: required {key} is not defined"
            for field_name, field_schema in properties.items():
                assert field_schema.get("type"), f"{name}.{field_name} has no type"
                assert field_schema.get("description"), (
                    f"{name}.{field_name} has no description — the model reads "
                    "these to decide what to pass"
                )
                if "enum" in field_schema:
                    assert field_schema["enum"], f"{name}.{field_name} empty enum"

    def test_timeframe_enum_matches_the_supported_table(
        self, bridge: ModuleType, server: Any
    ) -> None:
        response = server.handle(bridge.MCPRequest(method="tools/list", id=1))
        tools = {t["name"]: t for t in response.result["tools"]}
        enum = tools["mt5_rates"]["inputSchema"]["properties"]["timeframe"]["enum"]
        assert enum == list(bridge.TIMEFRAMES)

    def test_stop_loss_is_required_only_when_stops_are_required(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        def required(**kwargs: Any) -> List[str]:
            srv = bridge.build_server(stub, allow_trading=True, **kwargs)
            response = srv.handle(bridge.MCPRequest(method="tools/list", id=1))
            tools = {t["name"]: t for t in response.result["tools"]}
            return tools["mt5_order_send"]["inputSchema"]["required"]

        assert "sl" in required(require_stops=True)
        assert "sl" not in required(require_stops=False)

    def test_descriptions_explain_why_not_just_what(
        self, server: Any, bridge: ModuleType
    ) -> None:
        response = server.handle(bridge.MCPRequest(method="tools/list", id=1))
        for tool in response.result["tools"]:
            assert len(tool["description"]) > 60, tool["name"]


# ---------------------------------------------------------------------------
# Safety gates
# ---------------------------------------------------------------------------


class TestSafetyGates:
    def test_order_send_is_unknown_without_the_flag(self, server: Any) -> None:
        out = _call(server, "mt5_order_send", symbol="EURUSD", side="buy", volume=0.1)
        assert out["isError"] is True

    def test_a_real_account_is_refused_whatever_the_flags(
        self, bridge: ModuleType
    ) -> None:
        terminal = bridge.StubTerminal(trade_mode="real", now_fn=lambda: FIXED_NOW)
        server = bridge.build_server(terminal, allow_trading=True)
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.1,
            sl=1.0700,
        )
        assert out["isError"] is True
        assert "trade_mode='real'" in out["text"]
        assert "_assert_demo_account" in out["text"]

    def test_a_contest_account_is_refused_too(self, bridge: ModuleType) -> None:
        terminal = bridge.StubTerminal(trade_mode="contest", now_fn=lambda: FIXED_NOW)
        server = bridge.build_server(terminal, allow_trading=True)
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.1,
            sl=1.0700,
        )
        assert out["isError"] is True
        assert "contest" in out["text"]

    def test_no_stop_loss_no_trade(self, bridge: ModuleType, stub: Any) -> None:
        server = bridge.build_server(stub, allow_trading=True, require_stops=False)
        # With the requirement lifted the order goes through...
        ok = _call(server, "mt5_order_send", symbol="EURUSD", side="buy", volume=0.1)
        assert ok["isError"] is False
        # ...and with it in place the same call is refused.
        strict = bridge.build_server(stub, allow_trading=True)
        out = _call(
            strict,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.1,
        )
        assert out["isError"] is True
        assert "sl" in out["text"]

    def test_volume_must_be_a_lot_step_multiple(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.113,
            sl=1.0700,
        )
        assert out["isError"] is True
        assert "lot step 0.01" in out["text"]
        assert "0.11 or 0.12" in out["text"]

    def test_volume_limits_are_enforced(self, bridge: ModuleType, stub: Any) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        too_big = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=500.0,
            sl=1.0700,
        )
        assert too_big["isError"] is True
        assert "above the maximum" in too_big["text"]

        too_small = _call(
            server,
            "mt5_order_send",
            symbol="BTCUSD",
            side="buy",
            volume=0.001,
            sl=1.0,
        )
        assert too_small["isError"] is True
        # 0.001 is not a multiple of BTCUSD's 0.01 step either — both are true,
        # the volume check runs first and says so.
        assert (
            "lot step" in too_small["text"] or "below the minimum" in too_small["text"]
        )

    def test_stop_must_be_on_the_right_side_of_the_entry(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        ask = stub.tick("EURUSD")["ask"]
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.1,
            sl=round(ask + 0.0010, 5),
        )
        assert out["isError"] is True
        assert "must be below the entry" in out["text"]

    def test_stop_must_respect_the_broker_stops_level(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        ask = stub.tick("EURUSD")["ask"]
        # EURUSD's stub stops level is 10 points; four is too tight.
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.1,
            sl=round(ask - 0.00004, 5),
        )
        assert out["isError"] is True
        assert "10 points" in out["text"]

    def test_take_profit_side_is_checked(self, bridge: ModuleType, stub: Any) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        ask = stub.tick("EURUSD")["ask"]
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.1,
            sl=round(ask - 0.0010, 5),
            tp=round(ask - 0.0005, 5),
        )
        assert out["isError"] is True
        assert "take profit must be above" in out["text"]

    def test_sell_side_rules_are_mirrored(self, bridge: ModuleType, stub: Any) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        bid = stub.tick("EURUSD")["bid"]
        bad_sl = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="sell",
            volume=0.1,
            sl=round(bid - 0.0010, 5),
        )
        assert bad_sl["isError"] is True
        assert "must be above the entry" in bad_sl["text"]

        bad_tp = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="sell",
            volume=0.1,
            sl=round(bid + 0.0010, 5),
            tp=round(bid + 0.0005, 5),
        )
        assert bad_tp["isError"] is True
        assert "take profit must be below" in bad_tp["text"]

    def test_a_valid_demo_order_fills_and_is_visible(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        ask = stub.tick("EURUSD")["ask"]
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.25,
            sl=round(ask - 0.0020, 5),
            tp=round(ask + 0.0040, 5),
            magic=4242,
            comment="x" * 60,
        )
        assert out["isError"] is False, out["text"]
        payload = json.loads(out["text"])
        assert payload["result"]["retcode"] == 10009
        assert payload["result"]["retcode_message"] == "done"
        assert payload["account"]["trade_mode"] == "demo"
        assert "DEMO" in payload["warning"]
        assert len(payload["request"]["comment"]) == 31, "comments are capped"

        ticket = payload["result"]["deal"]
        positions = _payload(server, "mt5_positions", magic=4242)
        assert positions["count"] == 1
        opened = positions["positions"][0]
        assert opened["ticket"] == ticket
        assert opened["volume"] == 0.25
        assert opened["side"] == "buy"

    def test_prices_are_normalized_to_the_tick_size(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        ask = stub.tick("EURUSD")["ask"]
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="buy",
            volume=0.1,
            sl=round(ask - 0.00201234, 8),
        )
        assert out["isError"] is False, out["text"]
        request = json.loads(out["text"])["request"]
        asked = round(ask - 0.00201234, 8)
        assert request["sl"] == round(request["sl"], 5), "not on the 5-digit grid"
        assert request["sl"] != asked, "an off-grid price must be normalized"
        assert abs(request["sl"] - asked) < 1e-5, "but only by less than a point"

    def test_an_off_grid_price_is_snapped_to_the_tick_size(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        out = _call(
            server,
            "mt5_order_send",
            symbol="XAUUSD",
            side="buy",
            volume=0.1,
            price=2350.004,  # gold quotes to 2 decimals
            sl=2340.00,
        )
        assert out["isError"] is False, out["text"]
        request = json.loads(out["text"])["request"]
        assert request["price"] == 2350.0
        assert request["sl"] == 2340.0

    def test_a_price_far_from_the_market_is_rejected(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        out = _call(
            server,
            "mt5_order_send",
            symbol="XAUUSD",
            side="buy",
            volume=0.1,
            price=1.0,  # invented, not a quote
            sl=0.9,
        )
        assert out["isError"] is True
        assert "too far for a market order" in out["text"]
        assert "mt5_tick" in out["text"]

    def test_side_is_validated(self, bridge: ModuleType, stub: Any) -> None:
        server = bridge.build_server(stub, allow_trading=True)
        out = _call(
            server,
            "mt5_order_send",
            symbol="EURUSD",
            side="hold",
            volume=0.1,
            sl=1.07,
        )
        assert out["isError"] is True
        assert "side must be 'buy' or 'sell'" in out["text"]


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------


class TestArguments:
    def test_unknown_arguments_are_reported_not_ignored(self, server: Any) -> None:
        out = _call(server, "mt5_tick", symbol="EURUSD", bogus=1)
        assert out["isError"] is True
        assert "Unknown argument(s): bogus" in out["text"]

    def test_missing_required_arguments_are_reported(self, server: Any) -> None:
        out = _call(server, "mt5_tick")
        assert out["isError"] is True
        assert "Missing required argument(s): symbol" in out["text"]

    def test_empty_string_counts_as_missing(self, server: Any) -> None:
        out = _call(server, "mt5_tick", symbol="")
        assert out["isError"] is True
        assert "Missing required argument(s): symbol" in out["text"]

    def test_counts_are_clamped_not_trusted(self, server: Any) -> None:
        assert (
            _payload(server, "mt5_rates", symbol="EURUSD", count=99999)["count"] == 2000
        )
        assert _payload(server, "mt5_rates", symbol="EURUSD", count=0)["count"] == 1
        assert _payload(server, "mt5_symbols", limit=10_000)["count"] == 5
        assert _payload(server, "mt5_symbols", limit=-3)["count"] == 1

    def test_errors_from_the_backend_reach_the_agent_verbatim(
        self, server: Any
    ) -> None:
        out = _call(server, "mt5_tick", symbol="NOPE")
        assert out["isError"] is True
        assert "stub market has" in out["text"]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_initialize_identifies_the_bridge(
        self, server: Any, bridge: ModuleType
    ) -> None:
        response = server.handle(bridge.MCPRequest(method="initialize", id=7))
        result = response.result
        assert result["protocolVersion"] == "2025-11-25"
        assert result["serverInfo"]["name"] == "mt5"
        assert result["serverInfo"]["title"] == "MetaTrader 5 bridge"
        assert "tools" in result["capabilities"]

    def test_unknown_method_is_method_not_found(
        self, bridge: ModuleType, server: Any
    ) -> None:
        response = server.handle(bridge.MCPRequest(method="resources/list", id=3))
        assert response.error is not None
        from openjarvis.mcp.protocol import METHOD_NOT_FOUND

        assert response.error["code"] == METHOD_NOT_FOUND

    def test_dispatch_line_answers_requests_and_skips_notifications(
        self, bridge: ModuleType, server: Any
    ) -> None:
        line = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        response = bridge.dispatch_line(server, line)
        assert response is not None
        assert json.loads(response.to_json())["id"] == 1

        notification = json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        assert bridge.dispatch_line(server, notification) is None
        assert bridge.dispatch_line(server, "   ") is None

    def test_dispatch_line_reports_bad_input_as_json_rpc_errors(
        self, bridge: ModuleType, server: Any
    ) -> None:
        broken = bridge.dispatch_line(server, "{not json")
        assert broken is not None and broken.error is not None
        from openjarvis.mcp.protocol import PARSE_ERROR

        assert broken.error["code"] == PARSE_ERROR

        not_object = bridge.dispatch_line(server, "[1, 2, 3]")
        assert not_object is not None and not_object.error is not None
        from openjarvis.mcp.protocol import INVALID_REQUEST

        assert not_object.error["code"] == INVALID_REQUEST

        no_method = bridge.dispatch_line(server, '{"jsonrpc": "2.0", "id": 4}')
        assert no_method is not None and no_method.error is not None
        assert "method" in no_method.error["message"]

    def test_tool_results_are_json_the_agent_can_parse(self, server: Any) -> None:
        out = _call(server, "mt5_status")
        assert out["isError"] is False
        parsed = json.loads(out["text"])
        assert parsed["backend"] == "stub"
        # compact separators: this text lands in a context window
        assert "\n" not in out["text"] and ": " not in out["text"]

    def test_the_password_is_never_a_command_line_flag(
        self, bridge: ModuleType
    ) -> None:
        names = {param.name for param in bridge.main.params}
        opts = {
            opt for param in bridge.main.params for opt in getattr(param, "opts", [])
        }
        assert "password" not in names
        assert not any("password" in opt and "env" not in opt for opt in opts)
        assert "password_env" in names

    def test_http_mode_refuses_to_bind_publicly_without_a_token(
        self, bridge: ModuleType, server: Any
    ) -> None:
        import click

        with pytest.raises(click.ClickException) as excinfo:
            bridge.serve_http(server, host="0.0.0.0", port=0, token=None)
        assert "--token" in str(excinfo.value)

    def test_real_backend_explains_itself_without_metatrader(
        self, bridge: ModuleType
    ) -> None:
        if importlib.util.find_spec("MetaTrader5") is not None:
            pytest.skip("MetaTrader5 is importable here; nothing to explain")
        terminal = bridge.MetaTraderTerminal()
        with pytest.raises(bridge.Mt5Error) as excinfo:
            terminal.connect()
        message = str(excinfo.value)
        assert "Windows-only" in message
        assert "--stub" in message
        assert "--http" in message


# ---------------------------------------------------------------------------
# Config wiring: a bridge nobody can see is not a feature
# ---------------------------------------------------------------------------

PRESET = REPO_ROOT / "configs" / "openjarvis" / "examples" / "mql-assistant.toml"


class TestPresetWiring:
    def test_the_preset_lists_every_read_only_bridge_tool(
        self, bridge: ModuleType
    ) -> None:
        """`[tools] enabled` filters MCP tools by name — the preset must opt in."""
        from openjarvis.cli._tool_names import resolve_tool_names
        from openjarvis.core.config import load_config

        cfg = load_config(PRESET)
        allowed = set(resolve_tool_names(None, cfg.tools.enabled))
        server = bridge.build_server(bridge.StubTerminal(now_fn=lambda: FIXED_NOW))
        exposed = {tool.spec.name for tool in server.get_tools()}
        missing = exposed - allowed
        assert not missing, (
            f"the preset's [tools] enabled would hide {sorted(missing)} from the "
            "agent: a non-empty enabled list filters MCP tools by name"
        )

    def test_the_write_tool_stays_out_of_the_preset(self) -> None:
        from openjarvis.cli._tool_names import resolve_tool_names
        from openjarvis.core.config import load_config

        allowed = set(resolve_tool_names(None, load_config(PRESET).tools.enabled))
        assert "mt5_order_send" not in allowed

    def test_the_preset_documents_a_command_that_exists(self) -> None:
        text = PRESET.read_text(encoding="utf-8")
        assert "examples/mql_companion/mt5_mcp_server.py" in text
        assert SCRIPT.is_file()
        # both transports are documented, because the split terminal/VPS setup
        # is the common real-world one
        assert "--http" in text and '"url"' in text


# ---------------------------------------------------------------------------
# Strategy Tester reports: the numbers behind an EA, without a terminal
# ---------------------------------------------------------------------------

# Two label/value pairs per row, as the real report writes them.
TESTER_HTML = (
    "<html><body><table>"
    "<tr><td>Expert Advisor</td><td>MyEA.ex5</td>"
    "<td>Symbol</td><td>EURUSD</td></tr>"
    "<tr><td>Total Net Profit</td><td>1 234.56</td>"
    "<td>Gross Profit</td><td>4 500.00</td></tr>"
    "<tr><td>Gross Loss</td><td>-3 265.44</td>"
    "<td>Profit Factor</td><td>1.38</td></tr>"
    "<tr><td>Sharpe Ratio</td><td>0.86</td>"
    "<td>History Quality</td><td>100%</td></tr>"
    "<tr><td>Total Trades</td><td>243</td>"
    "<td>Equity Drawdown Maximal</td><td>812.44 (8.01%)</td></tr>"
    "<tr><td>Equity Drawdown Relative</td><td>8.01%</td></tr>"
    "</table></body></html>"
)


@pytest.fixture()
def tester_report(tmp_path: Path) -> Path:
    path = tmp_path / "TesterReport.htm"
    path.write_text(TESTER_HTML, encoding="utf-8")
    return path


class TestTesterTools:
    """These read files, so they work in --stub mode with no terminal at all."""

    def test_the_readers_are_registered_by_default(
        self, bridge: ModuleType, server: Any
    ) -> None:
        response = server.handle(bridge.MCPRequest(method="tools/list", id=1))
        tools = {t["name"]: t for t in response.result["tools"]}
        for name in ("mt5_tester_report", "mt5_tester_compare"):
            assert name in tools
            assert tools[name]["annotations"]["readOnlyHint"] is True

    def test_running_a_backtest_needs_an_explicit_flag(
        self, bridge: ModuleType, stub: Any
    ) -> None:
        """Launching a terminal closes it afterwards; that is opt-in."""
        plain = bridge.build_server(stub)
        names = {t.spec.name for t in plain.get_tools()}
        assert "mt5_tester_run" not in names

        allowed = bridge.build_server(stub, allow_tester_run=True)
        tools = {t.spec.name: t for t in allowed.get_tools()}
        assert "mt5_tester_run" in tools
        assert tools["mt5_tester_run"].spec.metadata["read_only"] is False

    def test_run_is_unknown_without_the_flag(self, server: Any) -> None:
        out = _call(server, "mt5_tester_run", expert="MyEA")
        assert out["isError"] is True

    def test_report_metrics_come_back_as_numbers(
        self, server: Any, tester_report: Path
    ) -> None:
        payload = _payload(server, "mt5_tester_report", path=str(tester_report))
        metrics = payload["metrics"]
        assert metrics["net_profit"] == 1234.56
        assert metrics["profit_factor"] == 1.38
        assert metrics["gross_loss"] == -3265.44, "MT5 reports losses as negative"
        assert metrics["equity_drawdown"] == 812.44
        assert metrics["equity_drawdown_pct"] == 8.01
        assert metrics["equity_drawdown_relative_pct"] == 8.01
        assert payload["found_by"] == "explicit"

    def test_a_prompt_summary_is_included_by_default(
        self, server: Any, tester_report: Path
    ) -> None:
        payload = _payload(server, "mt5_tester_report", path=str(tester_report))
        assert "profit_factor: 1.38" in payload["summary"]

    def test_thresholds_turn_the_call_into_a_gate(
        self, server: Any, tester_report: Path
    ) -> None:
        passed = _payload(
            server,
            "mt5_tester_report",
            path=str(tester_report),
            min_profit_factor=1.2,
            min_trades=100,
        )
        assert passed["thresholds"]["all_passed"] is True

        failed = _payload(
            server, "mt5_tester_report", path=str(tester_report), min_sharpe_ratio=1.5
        )
        assert failed["thresholds"]["all_passed"] is False
        assert failed["thresholds"]["failed"] == 1

    def test_the_newest_report_is_found_when_no_path_is_given(
        self, bridge: ModuleType, stub: Any, tmp_path: Path
    ) -> None:
        (tmp_path / "old.htm").write_text(TESTER_HTML, encoding="utf-8")
        os.utime(tmp_path / "old.htm", (1_000_000, 1_000_000))
        newest = tmp_path / "new.htm"
        newest.write_text(TESTER_HTML.replace("1.38", "2.50"), encoding="utf-8")
        os.utime(newest, (2_000_000, 2_000_000))

        srv = bridge.build_server(stub, tester_dirs=[str(tmp_path)])
        payload = _payload(srv, "mt5_tester_report")
        assert payload["found_by"] == "newest"
        assert payload["source"] == str(newest)
        assert payload["metrics"]["profit_factor"] == 2.5

    def test_search_dir_overrides_the_server_roots(
        self, bridge: ModuleType, stub: Any, tester_report: Path
    ) -> None:
        srv = bridge.build_server(stub, tester_dirs=["/nonexistent"])
        payload = _payload(
            srv, "mt5_tester_report", search_dir=str(tester_report.parent)
        )
        assert payload["source"] == str(tester_report)

    def test_nothing_found_says_where_it_looked(
        self, bridge: ModuleType, stub: Any, tmp_path: Path
    ) -> None:
        srv = bridge.build_server(stub, tester_dirs=[str(tmp_path)])
        out = _call(srv, "mt5_tester_report")
        assert out["isError"] is True
        assert str(tmp_path) in out["text"]
        assert "--tester-dir" in out["text"], "the fix must be actionable"

    def test_a_bad_path_lists_the_reports_that_do_exist(
        self, bridge: ModuleType, stub: Any, tester_report: Path
    ) -> None:
        srv = bridge.build_server(stub, tester_dirs=[str(tester_report.parent)])
        out = _call(srv, "mt5_tester_report", path="/nope/missing.htm")
        assert out["isError"] is True
        assert tester_report.name in out["text"]

    def test_compare_needs_two_reports(self, server: Any, tester_report: Path) -> None:
        out = _call(server, "mt5_tester_compare", paths=[str(tester_report)])
        assert out["isError"] is True
        assert "at least two" in out["text"]

    def test_compare_returns_deltas_and_a_verdict(
        self, server: Any, tester_report: Path, tmp_path: Path
    ) -> None:
        better = tmp_path / "better.htm"
        better.write_text(
            TESTER_HTML.replace("1 234.56", "2 500.00").replace("1.38", "1.62"),
            encoding="utf-8",
        )
        payload = _payload(
            server,
            "mt5_tester_compare",
            paths=[str(tester_report), str(better)],
            keys=["net_profit", "profit_factor"],
        )
        rows = {row["metric"]: row for row in payload["comparison"]["rows"]}
        assert rows["net_profit"]["delta"] == pytest.approx(1265.44)
        assert rows["profit_factor"]["delta"] == pytest.approx(0.24)
        assert payload["comparison"]["verdict"]["net_profit"]["winner"] == str(better)
        assert "missing" in payload["note"].lower() or "delta" in payload["note"]

    def test_compare_rejects_a_missing_file(self, server: Any, tmp_path: Path) -> None:
        first = tmp_path / "a.htm"
        first.write_text(TESTER_HTML, encoding="utf-8")
        out = _call(
            server, "mt5_tester_compare", paths=[str(first), str(tmp_path / "b.htm")]
        )
        assert out["isError"] is True

    def test_scan_log_finds_the_ea_errors(
        self, bridge: ModuleType, stub: Any, tester_report: Path
    ) -> None:
        logs = tester_report.parent / "Tester" / "logs"
        logs.mkdir(parents=True)
        (logs / "20240601.log").write_text(
            "error: failed to open position\nok line\n", encoding="utf-8"
        )
        srv = bridge.build_server(stub, tester_dirs=[str(tester_report.parent)])
        payload = _payload(
            srv,
            "mt5_tester_report",
            path=str(tester_report),
            scan_log=True,
        )
        assert payload["log"]["error_count"] == 1

    def test_run_launches_the_terminal_and_parses_what_it_wrote(
        self,
        bridge: ModuleType,
        stub: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The runner is exercised with a fake terminal that writes a report."""
        tester_lib = bridge.tester_lib
        assert tester_lib is not None

        terminal = tmp_path / "terminal64"
        terminal.write_text(
            "#!/usr/bin/env bash\n"
            'cfg=""; for a in "$@"; do case "$a" in /config:*) '
            'cfg="${a#/config:}"; cfg="${cfg%\\"}"; cfg="${cfg#\\"}";; esac; done\n'
            'report=$(grep -m1 "^Report=" "$cfg" | cut -d= -f2-)\n'
            'printf "%s" "<?xml version=\'1.0\'?><Report>'
            "<Total_Net_Profit>500.00</Total_Net_Profit>"
            "<Gross_Profit>900.00</Gross_Profit><Gross_Loss>-400.00</Gross_Loss>"
            "<Profit_Factor>2.25</Profit_Factor><Total_Trades>40</Total_Trades>"
            '</Report>" > "${report}.xml"\n',
            encoding="utf-8",
        )
        terminal.chmod(0o755)
        monkeypatch.setattr(tester_lib, "find_terminal", lambda explicit=None: terminal)

        srv = bridge.build_server(stub, allow_tester_run=True)
        target = tmp_path / "out" / "Run.xml"
        payload = _payload(
            srv,
            "mt5_tester_run",
            expert="MyEA",
            symbol="EURUSD",
            period="H1",
            report_path=str(target),
            timeout=60,
        )
        assert payload["metrics"]["net_profit"] == 500.0
        assert payload["metrics"]["profit_factor"] == 2.25
        assert payload["run"]["exit_code"] == 0
        assert "Expert=MyEA" in payload["ini"]

    def test_run_reports_a_missing_terminal_clearly(
        self, bridge: ModuleType, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tester_lib = bridge.tester_lib
        assert tester_lib is not None
        monkeypatch.setattr(tester_lib, "find_terminal", lambda explicit=None: None)
        srv = bridge.build_server(stub, allow_tester_run=True)
        out = _call(srv, "mt5_tester_run", expert="MyEA")
        assert out["isError"] is True
        assert "TERMINAL_PATH" in out["text"]

    def test_the_tools_survive_a_missing_sibling_module(
        self, bridge: ModuleType, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bridge is often deployed as a single file; nothing may crash."""
        monkeypatch.setattr(bridge, "tester_lib", None)
        srv = bridge.build_server(stub)
        names = {t.spec.name for t in srv.get_tools()}
        assert "mt5_tester_report" not in names
        assert "mt5_status" in names, "the live-market tools are unaffected"
        out = _call(srv, "mt5_tester_report")
        assert out["isError"] is True


# ---------------------------------------------------------------------------
# End to end: a real subprocess speaking to the framework's own client
# ---------------------------------------------------------------------------


class TestStdioTransport:
    def _spawn(self, *extra: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(SCRIPT), *extra],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
        )

    def test_stdout_carries_nothing_but_json_rpc(self) -> None:
        proc = self._spawn("--stub")
        assert proc.stdin is not None and proc.stdout is not None
        lines = [
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            ),
            json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "mt5_tick", "arguments": {"symbol": "EURUSD"}},
                }
            ),
        ]
        try:
            out, err = proc.communicate("\n".join(lines) + "\n", timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - would be a bug
            proc.kill()
            pytest.fail("the bridge did not answer three requests in 60s")

        responses = [line for line in out.splitlines() if line.strip()]
        assert len(responses) == 3, (
            f"expected 3 responses (no reply to a notification), got {responses}"
        )
        ids = []
        for line in responses:
            message = json.loads(line)  # raises if stdout carried anything else
            assert message["jsonrpc"] == "2.0"
            ids.append(message["id"])
        assert ids == [1, 2, 3]
        assert "MetaTrader 5 bridge" in out
        # the third response is the tick payload, not the tool name
        assert "EURUSD" in responses[2]
        assert "spread_points" in responses[2]
        assert proc.returncode == 0
        assert "serving MCP over stdio" in err, "logging must go to stderr"

    def test_the_framework_client_can_drive_it(self) -> None:
        from openjarvis.mcp.client import MCPClient
        from openjarvis.mcp.transport import StdioTransport

        command = [sys.executable, str(SCRIPT), "--stub", "--allow-trading"]
        with MCPClient(StdioTransport(command)) as client:
            info = client.initialize()
            assert info["serverInfo"]["name"] == "mt5"
            tools = {tool.name for tool in client.list_tools()}
            assert "mt5_order_send" in tools
            assert tools >= {"mt5_status", "mt5_tick", "mt5_calc"}

            result = client.call_tool("mt5_account", {})
            assert result["isError"] is False
            account = json.loads(result["content"][0]["text"])
            assert account["trade_mode"] == "demo"

            result = client.call_tool("mt5_calc", {"symbol": "EURUSD", "volume": 0.1})
            calc = json.loads(result["content"][0]["text"])
            assert calc["margin_per_lot"] > 0

    def test_it_loads_through_the_config_path_openjarvis_uses(self) -> None:
        from openjarvis.mcp.loader import load_mcp_tools_from_config

        config = SimpleNamespace(
            enabled=True,
            servers=json.dumps(
                [
                    {
                        "name": "mt5",
                        "command": sys.executable,
                        "args": [str(SCRIPT), "--stub"],
                        "exclude_tools": ["mt5_orders"],
                    }
                ]
            ),
        )
        tools, clients = load_mcp_tools_from_config(config)
        try:
            names = sorted(tool.spec.name for tool in tools)
            assert "mt5_orders" not in names
            assert "mt5_order_send" not in names
            assert "mt5_tick" in names
            tick = next(t for t in tools if t.spec.name == "mt5_tick")
            assert tick.spec.timeout_seconds == 600.0
            result = tick.execute(symbol="GBPUSD")
            assert result.success
            assert json.loads(result.content)["symbol"] == "GBPUSD"
        finally:
            for client in clients:
                client.close()


# ---------------------------------------------------------------------------
# Optimization reports: ranked passes, and the .set that reproduces one
# ---------------------------------------------------------------------------

OPTIMIZATION_XML = (
    "\n".join(
        (
            '<?xml version="1.0" encoding="ANSI"?>',
            "<Table>",
            "  <Row>"
            "<Cell>Pass</Cell><Cell>Result</Cell><Cell>Profit</Cell>"
            "<Cell>Expected Payoff</Cell><Cell>Profit Factor</Cell>"
            "<Cell>Recovery Factor</Cell><Cell>Sharpe Ratio</Cell>"
            "<Cell>Custom</Cell><Cell>Equity DD %</Cell><Cell>Trades</Cell>"
            "<Cell>InpFastEMA</Cell><Cell>InpStopLoss</Cell>"
            "</Row>",
            "  <Row>"
            "<Cell>7</Cell><Cell>108000</Cell><Cell>98000</Cell>"
            "<Cell>8166.67</Cell><Cell>14.86</Cell><Cell>30.17</Cell>"
            "<Cell>1.63</Cell><Cell>0</Cell><Cell>32.48</Cell><Cell>12</Cell>"
            "<Cell>5</Cell><Cell>1000</Cell>"
            "</Row>",
            "  <Row>"
            "<Cell>3</Cell><Cell>11300</Cell><Cell>1300</Cell>"
            "<Cell>6.5</Cell><Cell>1.5</Cell><Cell>2.1</Cell>"
            "<Cell>3.5</Cell><Cell>0</Cell><Cell>9</Cell><Cell>200</Cell>"
            "<Cell>12</Cell><Cell>1000</Cell>"
            "</Row>",
            "  <Row>"
            "<Cell>4</Cell><Cell>11200</Cell><Cell>1200</Cell>"
            "<Cell>6</Cell><Cell>1.4</Cell><Cell>2</Cell>"
            "<Cell>1.1</Cell><Cell>0</Cell><Cell>10</Cell><Cell>200</Cell>"
            "<Cell>20</Cell><Cell>1000</Cell>"
            "</Row>",
            "  <Row>"
            "<Cell>5</Cell><Cell>9000</Cell><Cell>-1000</Cell>"
            "<Cell>-5</Cell><Cell>0.9</Cell><Cell>-0.5</Cell>"
            "<Cell>-1</Cell><Cell>0</Cell><Cell>60</Cell><Cell>0</Cell>"
            "<Cell>25</Cell><Cell>500</Cell>"
            "</Row>",
            "</Table>",
        )
    )
    + "\n"
)

OPTIMIZATION_SET = (
    "\n".join(
        (
            "InpFastEMA=12||5||1||30||Y",
            "InpStopLoss=500||200||50||1000||Y",
            "InpLots=0.10||0||0||0||N",
        )
    )
    + "\n"
)


@pytest.fixture()
def optimization_report(tmp_path: Path) -> Path:
    path = tmp_path / "ReportOptimizer-555849.xml"
    path.write_bytes(OPTIMIZATION_XML.encode("cp1251"))
    return path


@pytest.fixture()
def optimization_set(tmp_path: Path) -> Path:
    path = tmp_path / "grid.set"
    path.write_bytes(OPTIMIZATION_SET.encode("cp1251"))
    return path


class TestTesterOptimizationTool:
    def test_it_is_registered_read_only(self, bridge: ModuleType, server: Any) -> None:
        response = server.handle(bridge.MCPRequest(method="tools/list", id=1))
        tools = {t["name"]: t for t in response.result["tools"]}
        assert "mt5_tester_optimization" in tools
        assert tools["mt5_tester_optimization"]["annotations"]["readOnlyHint"] is True

    def test_it_ranks_the_passes(self, server: Any, optimization_report: Path) -> None:
        payload = _payload(
            server, "mt5_tester_optimization", path=str(optimization_report)
        )
        assert payload["passes"] == 4
        assert payload["parameter_names"] == ["InpFastEMA", "InpStopLoss"]
        assert payload["found_by"] == "explicit"
        analysis = payload["analysis"]
        assert analysis["kept"] == 3
        assert analysis["dropped"] == 1
        assert analysis["top"][0]["pass"] == 7
        assert analysis["top"][0]["inputs"]["InpStopLoss"] == 1000
        assert any("traded 12 time" in w for w in analysis["warnings"])
        assert "A pass is not a backtest" in payload["note"]

    def test_the_summary_is_there_unless_asked_otherwise(
        self, server: Any, optimization_report: Path
    ) -> None:
        payload = _payload(
            server, "mt5_tester_optimization", path=str(optimization_report)
        )
        assert "passes: 4 total" in payload["summary"]
        quiet = _payload(
            server,
            "mt5_tester_optimization",
            path=str(optimization_report),
            summary=False,
        )
        assert "summary" not in quiet

    def test_rank_by_and_top_are_honoured(
        self, server: Any, optimization_report: Path
    ) -> None:
        payload = _payload(
            server,
            "mt5_tester_optimization",
            path=str(optimization_report),
            rank_by="sharpe",
            top=2,
        )
        assert len(payload["analysis"]["top"]) == 2
        assert payload["analysis"]["top"][0]["pass"] == 3
        assert payload["analysis"]["rank_by"] == "sharpe_ratio"

    def test_the_junk_filter_can_be_turned_off(
        self, server: Any, optimization_report: Path
    ) -> None:
        payload = _payload(
            server,
            "mt5_tester_optimization",
            path=str(optimization_report),
            filter_junk=False,
        )
        assert payload["analysis"]["kept"] == 4
        assert payload["analysis"]["dropped"] == 0

    def test_the_set_file_supplies_the_ranges(
        self, server: Any, optimization_report: Path, optimization_set: Path
    ) -> None:
        payload = _payload(
            server,
            "mt5_tester_optimization",
            path=str(optimization_report),
            set_path=str(optimization_set),
            top=3,
        )
        assert payload["set_file"]["grid_combinations"] == 26 * 17
        assert any(
            "InpStopLoss sits at the stop of its tested range" in w
            for w in payload["analysis"]["warnings"]
        )

    def test_set_from_pass_returns_text_and_writes_nothing(
        self,
        server: Any,
        optimization_report: Path,
        optimization_set: Path,
        tmp_path: Path,
    ) -> None:
        before = sorted(item.name for item in tmp_path.iterdir())
        payload = _payload(
            server,
            "mt5_tester_optimization",
            path=str(optimization_report),
            set_path=str(optimization_set),
            set_from_pass=7,
        )
        generated = payload["set_from_pass"]
        assert generated["pass"] == 7
        assert "InpFastEMA=5||0||0||0||N" in generated["text"]
        # The inputs the optimization held fixed come from the template, or the
        # re-test would silently run on the EA's compiled defaults.
        assert "InpLots=0.10" in generated["text"]
        assert "MQL5/Profiles/Tester" in generated["hint"]
        assert sorted(item.name for item in tmp_path.iterdir()) == before

    def test_an_unknown_pass_number_is_an_error(
        self, server: Any, optimization_report: Path
    ) -> None:
        out = _call(
            server,
            "mt5_tester_optimization",
            path=str(optimization_report),
            set_from_pass=999,
        )
        assert out["isError"] is True
        assert "no pass 999" in out["text"]

    def test_it_refuses_a_single_test_report(
        self, server: Any, tester_report: Path
    ) -> None:
        out = _call(server, "mt5_tester_optimization", path=str(tester_report))
        assert out["isError"] is True
        assert "single-test report" in out["text"]
        assert "mt5_tester_report" in out["text"]

    def test_it_finds_the_newest_optimization_report(
        self,
        bridge: ModuleType,
        stub: Any,
        tester_report: Path,
        optimization_report: Path,
    ) -> None:
        srv = bridge.build_server(stub, tester_dirs=[str(optimization_report.parent)])
        payload = _payload(srv, "mt5_tester_optimization")
        assert payload["found_by"] == "newest"
        assert payload["source"] == str(optimization_report)

    def test_a_missing_file_explains_itself(self, server: Any) -> None:
        out = _call(server, "mt5_tester_optimization", path="/nope/missing.xml")
        assert out["isError"] is True
        assert "optimization report not found" in out["text"]

    def test_no_report_anywhere_explains_where_it_looked(
        self, bridge: ModuleType, stub: Any, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        srv = bridge.build_server(stub, tester_dirs=[str(empty)])
        out = _call(srv, "mt5_tester_optimization")
        assert out["isError"] is True
        assert "no optimization report found" in out["text"]

    def test_a_missing_set_file_is_an_error(
        self, server: Any, optimization_report: Path
    ) -> None:
        out = _call(
            server,
            "mt5_tester_optimization",
            path=str(optimization_report),
            set_path="/nope/grid.set",
        )
        assert out["isError"] is True
        assert ".set file not found" in out["text"]
