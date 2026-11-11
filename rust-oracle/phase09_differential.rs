//! Differential assertions against the exact upstream Raydium CLMM Rust source.
//! The Python workflow exporter writes the same input vectors used by the Python implementation.
use raydium_clmm::libraries::{swap_math, tick_math};
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
