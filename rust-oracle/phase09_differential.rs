//! Differential assertions against the exact upstream Raydium CLMM Rust source.
//! The Python workflow exporter writes the same input vectors used by the Python implementation.
use raydium_clmm::libraries::{swap_math, tick_math};
use raydium_clmm::states::{tick_spacing_index_from_tick, DynamicFeeInfo};
use serde_json::Value;

const PYTHON_VECTORS: &str = include_str!("phase09_python_vectors.json");

fn parse_u128(value: &Value) -> u128 {
    value
        .as_str()
        .expect("u128 vector values are encoded as decimal strings")
        .parse::<u128>()
        .expect("valid u128 decimal")
}

fn parse_u64(value: &Value) -> u64 {
    value.as_u64().expect("valid u64 vector value")
}

#[test]
fn python_tick_math_vectors_match_pinned_raydium_rust() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    assert_eq!(
        vectors["source_commit"].as_str(),
        Some("6d7a0f82d2f293c553c69aaf943377367c6c4cc6")
    );

    let ticks = vectors["ticks"].as_array().expect("tick vector array");
    assert!(!ticks.is_empty());
    for vector in ticks {
        let tick = vector["tick"].as_i64().expect("tick i32") as i32;
        let expected = parse_u128(&vector["sqrt_price_x64"]);
        let actual = tick_math::get_sqrt_price_at_tick(tick)
            .unwrap_or_else(|error| panic!("Rust tick math failed for tick {tick}: {error:?}"));
        assert_eq!(actual, expected, "sqrt price mismatch at tick {tick}");

        // Raydium deliberately excludes MAX_SQRT_PRICE_X64 from inverse lookup.
        if tick < 443636 {
            let inverse = tick_math::get_tick_at_sqrt_price(actual)
                .unwrap_or_else(|error| panic!("Rust inverse tick math failed at tick {tick}: {error:?}"));
            assert_eq!(inverse, tick, "inverse tick mismatch at tick {tick}");
        }
    }
}

#[test]
fn python_swap_step_vectors_match_pinned_raydium_rust() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["swap_steps"].as_array().expect("swap vector array");
    assert!(cases.len() >= 6);

    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let current = parse_u128(&case["sqrt_price_current_x64"]);
        let target = parse_u128(&case["sqrt_price_target_x64"]);
        let liquidity = parse_u64(&case["liquidity"]) as u128;
        let amount_remaining = parse_u64(&case["amount_remaining"]);
        let fee_rate = parse_u64(&case["fee_rate"]) as u32;
        let is_base_input = case["is_base_input"].as_bool().expect("base input flag");
        let zero_for_one = case["zero_for_one"].as_bool().expect("swap direction");
        let is_fee_on_input = case["is_fee_on_input"].as_bool().expect("fee side");

        let actual = swap_math::compute_swap(
            current,
            target,
            liquidity,
            amount_remaining,
            fee_rate,
            is_base_input,
            zero_for_one,
            is_fee_on_input,
        )
        .unwrap_or_else(|error| panic!("Rust swap math failed for {name}: {error:?}"));

        let expected = &case["expected"];
        assert_eq!(
            actual.sqrt_price_next_x64,
            parse_u128(&expected["sqrt_price_next_x64"]),
            "sqrt price mismatch for {name}"
        );
        assert_eq!(
            actual.amount_in,
            parse_u64(&expected["amount_in"]),
            "amount_in mismatch for {name}"
        );
        assert_eq!(
            actual.amount_out,
            parse_u64(&expected["amount_out"]),
            "amount_out mismatch for {name}"
        );
        assert_eq!(
            actual.fee_amount,
            parse_u64(&expected["fee_amount"]),
            "fee amount mismatch for {name}"
        );
        assert_eq!(
            actual.sqrt_price_next_x64 == target,
            expected["reached_target"].as_bool().expect("reached target flag"),
            "reached-target mismatch for {name}"
        );
    }
}


fn dynamic_info(value: &Value) -> DynamicFeeInfo {
    DynamicFeeInfo {
        filter_period: value["filter_period"].as_u64().expect("filter period") as u16,
        decay_period: value["decay_period"].as_u64().expect("decay period") as u16,
        reduction_factor: value["reduction_factor"].as_u64().expect("reduction factor") as u16,
        dynamic_fee_control: value["dynamic_fee_control"].as_u64().expect("dynamic fee control") as u32,
        max_volatility_accumulator: value["max_volatility_accumulator"].as_u64().expect("maximum volatility") as u32,
        tick_spacing_index_reference: value["tick_spacing_index_reference"].as_i64().expect("tick reference") as i32,
        volatility_reference: value["volatility_reference"].as_u64().expect("volatility reference") as u32,
        volatility_accumulator: value["volatility_accumulator"].as_u64().expect("volatility accumulator") as u32,
        last_update_timestamp: value["last_update_timestamp"].as_u64().expect("last update timestamp"),
        padding: [0u8; 46],
    }
}

#[test]
fn python_dynamic_fee_reference_updates_match_pinned_raydium_rust() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["dynamic_fee"]["reference_updates"]
        .as_array()
        .expect("dynamic fee reference cases");
    assert_eq!(cases.len(), 4);

    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let mut info = dynamic_info(&case["initial"]);
        let tick_index = case["tick_spacing_index"].as_i64().expect("tick spacing index") as i32;
        let timestamp = case["current_timestamp"].as_u64().expect("timestamp");
        info.update_reference(tick_index, timestamp)
            .unwrap_or_else(|error| panic!("Rust reference update failed for {name}: {error:?}"));
        let expected = &case["expected"];
        assert_eq!(
            info.tick_spacing_index_reference,
            expected["tick_spacing_index_reference"].as_i64().unwrap() as i32,
            "tick reference mismatch for {name}"
        );
        assert_eq!(
            info.volatility_reference,
            expected["volatility_reference"].as_u64().unwrap() as u32,
            "volatility reference mismatch for {name}"
        );
        assert_eq!(
            info.volatility_accumulator,
            expected["volatility_accumulator"].as_u64().unwrap() as u32,
            "volatility accumulator mismatch for {name}"
        );
        assert_eq!(
            info.last_update_timestamp,
            expected["last_update_timestamp"].as_u64().unwrap(),
            "last update timestamp mismatch for {name}"
        );
    }
}

#[test]
fn python_volatility_accumulator_updates_match_pinned_raydium_rust() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["dynamic_fee"]["volatility_updates"]
        .as_array()
        .expect("volatility update cases");
    assert_eq!(cases.len(), 2);

    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let mut info = DynamicFeeInfo {
            filter_period: 60,
            decay_period: 3600,
            reduction_factor: 5000,
            dynamic_fee_control: 1000,
            max_volatility_accumulator: case["initial"]["max_volatility_accumulator"].as_u64().unwrap() as u32,
            tick_spacing_index_reference: case["initial"]["tick_spacing_index_reference"].as_i64().unwrap() as i32,
            volatility_reference: case["initial"]["volatility_reference"].as_u64().unwrap() as u32,
            volatility_accumulator: 0,
            last_update_timestamp: 1000,
            padding: [0u8; 46],
        };
        let tick_index = case["tick_spacing_index"].as_i64().unwrap() as i32;
        info.update_volatility_accumulator(tick_index)
            .unwrap_or_else(|error| panic!("Rust volatility update failed for {name}: {error:?}"));
        assert_eq!(
            info.volatility_accumulator,
            case["expected_volatility_accumulator"].as_u64().unwrap() as u32,
            "volatility accumulator mismatch for {name}"
        );
    }
}

#[test]
fn python_tick_spacing_index_vectors_match_pinned_raydium_rust() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["dynamic_fee"]["tick_spacing_indexes"]
        .as_array()
        .expect("tick spacing index cases");
    assert_eq!(cases.len(), 6);

    for case in cases {
        let tick = case["tick"].as_i64().unwrap() as i32;
        let spacing = case["tick_spacing"].as_u64().unwrap() as u16;
        let expected = case["expected"].as_i64().unwrap() as i32;
        assert_eq!(
            tick_spacing_index_from_tick(tick, spacing),
            expected,
            "tick-spacing index mismatch for tick={tick}, spacing={spacing}"
        );
    }
}
