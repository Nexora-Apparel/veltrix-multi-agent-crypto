//! Differential assertions against the exact upstream Raydium CLMM Rust source.
//! The Python workflow exporter writes the same input vectors used by the Python implementation.
use raydium_clmm::libraries::{liquidity_math, swap_math, tick_math};
use raydium_clmm::instructions::swap::SwapState;
use raydium_clmm::states::{tick_spacing_index_from_tick, DynamicFeeInfo, PoolState, RewardInfo, TickState};
use anchor_lang::prelude::Pubkey;
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
        let actual_tick_reference = info.tick_spacing_index_reference;
        let actual_volatility_reference = info.volatility_reference;
        let actual_volatility_accumulator = info.volatility_accumulator;
        let actual_last_update_timestamp = info.last_update_timestamp;
        assert_eq!(
            actual_tick_reference,
            expected["tick_spacing_index_reference"].as_i64().unwrap() as i32,
            "tick reference mismatch for {name}"
        );
        assert_eq!(
            actual_volatility_reference,
            expected["volatility_reference"].as_u64().unwrap() as u32,
            "volatility reference mismatch for {name}"
        );
        assert_eq!(
            actual_volatility_accumulator,
            expected["volatility_accumulator"].as_u64().unwrap() as u32,
            "volatility accumulator mismatch for {name}"
        );
        assert_eq!(
            actual_last_update_timestamp,
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
        let actual_volatility_accumulator = info.volatility_accumulator;
        assert_eq!(
            actual_volatility_accumulator,
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


#[test]
fn python_dynamic_total_fee_rates_match_pinned_raydium_swap_state() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["dynamic_fee"]["total_fee_rates"]
        .as_array()
        .expect("dynamic total fee-rate cases");
    assert_eq!(cases.len(), 4);

    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let info = dynamic_info(&case["info"]);
        let state = SwapState {
            amount_specified_remaining: 1,
            amount_calculated: 0,
            sqrt_price_x64: 1u128 << 64,
            tick: 0,
            fee_growth_global_x64: 0,
            lp_fee: 0,
            protocol_fee: 0,
            fund_fee: 0,
            liquidity: 1,
            sqrt_price_next_x64: 0,
            tick_next: 0,
            tick_spacing: case["tick_spacing"].as_u64().unwrap() as u16,
            base_fee_rate: case["base_fee_rate"].as_u64().unwrap() as u32,
            tick_spacing_index: 0,
            dynamic_fee_info: Some(info),
        };
        let actual = state
            .get_total_fee_rate()
            .unwrap_or_else(|error| panic!("Rust total fee-rate calculation failed for {name}: {error:?}"));
        assert_eq!(
            actual,
            case["expected_total_fee_rate"].as_u64().unwrap() as u32,
            "total fee-rate mismatch for {name}"
        );
    }
}


#[test]
fn python_fifo_limit_order_vectors_match_pinned_raydium_tick_state() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["limit_orders"].as_array().expect("FIFO limit-order cases");
    assert_eq!(cases.len(), 6);

    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let mut tick = TickState {
            tick: 0,
            liquidity_net: 0,
            liquidity_gross: 0,
            fee_growth_outside_0_x64: 0,
            fee_growth_outside_1_x64: 0,
            reward_growths_outside_x64: [0u128; 3],
            order_phase: case["order_phase"].as_u64().unwrap(),
            orders_amount: case["orders_amount"].as_u64().unwrap(),
            part_filled_orders_remaining: case["part_filled_orders_remaining"].as_u64().unwrap(),
            unfilled_ratio_x64: parse_u128(&case["unfilled_ratio_x64"]),
            padding: [0u32; 3],
        };
        let result = tick
            .match_limit_order_with_sqrt_price(
                case["swap_amount"].as_u64().unwrap(),
                case["zero_for_one"].as_bool().unwrap(),
                case["is_base_input"].as_bool().unwrap(),
                case["fee_rate"].as_u64().unwrap() as u32,
                case["is_fee_on_input"].as_bool().unwrap(),
                1u128 << 64,
            )
            .unwrap_or_else(|error| panic!("Rust FIFO match failed for {name}: {error:?}"));
        let expected = &case["expected"];
        assert_eq!(result.amount_in, parse_u64(&expected["amount_in"]), "amount_in mismatch for {name}");
        assert_eq!(result.amount_out, parse_u64(&expected["amount_out"]), "amount_out mismatch for {name}");
        assert_eq!(result.amm_fee_amount, parse_u64(&expected["amm_fee_amount"]), "fee mismatch for {name}");
        let actual_orders_amount = tick.orders_amount;
        let actual_part_filled = tick.part_filled_orders_remaining;
        let actual_order_phase = tick.order_phase;
        let actual_unfilled_ratio = tick.unfilled_ratio_x64;
        assert_eq!(actual_orders_amount, parse_u64(&expected["orders_amount"]), "orders_amount mismatch for {name}");
        assert_eq!(
            actual_part_filled,
            parse_u64(&expected["part_filled_orders_remaining"]),
            "part-filled cohort mismatch for {name}"
        );
        assert_eq!(actual_order_phase, parse_u64(&expected["order_phase"]), "FIFO phase mismatch for {name}");
        assert_eq!(
            actual_unfilled_ratio,
            parse_u128(&expected["unfilled_ratio_x64"]),
            "unfilled ratio mismatch for {name}"
        );
        assert_eq!(
            tick.is_initialized(),
            expected["initialized"].as_bool().unwrap(),
            "initialized state mismatch for {name}"
        );
    }
}


#[test]
fn python_tick_crossing_fee_reward_and_liquidity_vectors_match_pinned_raydium_rust() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["tick_crossing"].as_array().expect("tick crossing cases");
    assert_eq!(cases.len(), 2);

    for case in cases {
        let zero_for_one = case["zero_for_one"].as_bool().unwrap();
        let tick_vector = &case["tick"];
        let mut tick = TickState {
            tick: tick_vector["tick_index"].as_i64().unwrap() as i32,
            liquidity_net: tick_vector["liquidity_net"].as_i64().unwrap() as i128,
            liquidity_gross: tick_vector["liquidity_gross"].as_u64().unwrap() as u128,
            fee_growth_outside_0_x64: parse_u128(&tick_vector["fee_growth_outside_0_x64"]),
            fee_growth_outside_1_x64: parse_u128(&tick_vector["fee_growth_outside_1_x64"]),
            reward_growths_outside_x64: [
                parse_u128(&tick_vector["reward_growths_outside_x64"][0]),
                parse_u128(&tick_vector["reward_growths_outside_x64"][1]),
                parse_u128(&tick_vector["reward_growths_outside_x64"][2]),
            ],
            order_phase: 0,
            orders_amount: 0,
            part_filled_orders_remaining: 0,
            unfilled_ratio_x64: 0,
            padding: [0u32; 3],
        };

        let reward_vectors = case["rewards"].as_array().unwrap();
        let reward0 = &reward_vectors[0];
        let reward1 = &reward_vectors[1];
        let reward2 = &reward_vectors[2];
        let rewards: [RewardInfo; 3] = [
            RewardInfo {
                token_mint: if reward0["initialized"].as_bool().unwrap() { Pubkey::new_unique() } else { Pubkey::default() },
                reward_growth_global_x64: parse_u128(&reward0["reward_growth_global_x64"]),
                ..RewardInfo::default()
            },
            RewardInfo {
                token_mint: if reward1["initialized"].as_bool().unwrap() { Pubkey::new_unique() } else { Pubkey::default() },
                reward_growth_global_x64: parse_u128(&reward1["reward_growth_global_x64"]),
                ..RewardInfo::default()
            },
            RewardInfo {
                token_mint: if reward2["initialized"].as_bool().unwrap() { Pubkey::new_unique() } else { Pubkey::default() },
                reward_growth_global_x64: parse_u128(&reward2["reward_growth_global_x64"]),
                ..RewardInfo::default()
            },
        ];

        let liquidity_net = tick.cross(
            parse_u128(&case["fee_growth_global_0_x64"]),
            parse_u128(&case["fee_growth_global_1_x64"]),
            &rewards,
        );
        let delta = if zero_for_one { -liquidity_net } else { liquidity_net };
        let liquidity_after = liquidity_math::add_delta(
            case["current_liquidity"].as_u64().unwrap() as u128,
            delta,
        )
        .unwrap_or_else(|error| panic!("Rust liquidity delta failed: {error:?}"));

        let actual_fee0 = tick.fee_growth_outside_0_x64;
        let actual_fee1 = tick.fee_growth_outside_1_x64;
        let actual_rewards = tick.reward_growths_outside_x64;
        let expected = &case["expected"];
        assert_eq!(liquidity_net, expected["liquidity_net"].as_i64().unwrap() as i128);
        assert_eq!(liquidity_after, expected["liquidity_after"].as_u64().unwrap() as u128);
        assert_eq!(actual_fee0, parse_u128(&expected["fee_growth_outside_0_x64"]));
        assert_eq!(actual_fee1, parse_u128(&expected["fee_growth_outside_1_x64"]));
        for index in 0..3 {
            assert_eq!(
                actual_rewards[index],
                parse_u128(&expected["reward_growths_outside_x64"][index]),
                "reward growth mismatch at slot {index}"
            );
        }
    }
}


#[test]
fn python_pool_post_swap_finalization_vectors_match_pinned_raydium_pool_state() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["pool_finalization"].as_array().expect("PoolState finalization cases");
    assert_eq!(cases.len(), 6);

    for case in cases {
        let fee_on = case["fee_on"].as_u64().unwrap() as u8;
        let zero_for_one = case["zero_for_one"].as_bool().unwrap();
        let initial_dynamic = DynamicFeeInfo {
            filter_period: 60,
            decay_period: 3600,
            reduction_factor: 5000,
            dynamic_fee_control: 1000,
            max_volatility_accumulator: 100000,
            tick_spacing_index_reference: 3,
            volatility_reference: 2000,
            volatility_accumulator: 4000,
            last_update_timestamp: 1000,
            padding: [0u8; 46],
        };
        let updated_dynamic = DynamicFeeInfo {
            filter_period: 60,
            decay_period: 3600,
            reduction_factor: 5000,
            dynamic_fee_control: 1000,
            max_volatility_accumulator: 100000,
            tick_spacing_index_reference: 10,
            volatility_reference: 2500,
            volatility_accumulator: 5000,
            last_update_timestamp: 1060,
            padding: [0u8; 46],
        };
        let mut pool = PoolState::default();
        pool.fee_on = fee_on;
        pool.fee_growth_global_0_x64 = 10;
        pool.fee_growth_global_1_x64 = 20;
        pool.protocol_fees_token_0 = 100;
        pool.protocol_fees_token_1 = 200;
        pool.fund_fees_token_0 = 300;
        pool.fund_fees_token_1 = 400;
        pool.dynamic_fee_info = initial_dynamic;

        pool.update_after_swap(
            case["tick"].as_i64().unwrap() as i32,
            parse_u128(&case["sqrt_price_x64"]),
            case["liquidity"].as_u64().unwrap() as u128,
            case["lp_fee"].as_u64().unwrap(),
            case["protocol_fee"].as_u64().unwrap(),
            case["fund_fee"].as_u64().unwrap(),
            parse_u128(&case["fee_growth_global_x64"]),
            zero_for_one,
            Some(updated_dynamic),
        )
        .unwrap_or_else(|error| panic!("Rust PoolState finalization failed: {error:?}"));

        let actual_tick = pool.tick_current;
        let actual_sqrt_price = pool.sqrt_price_x64;
        let actual_liquidity = pool.liquidity;
        let actual_fee_growth_0 = pool.fee_growth_global_0_x64;
        let actual_fee_growth_1 = pool.fee_growth_global_1_x64;
        let actual_protocol_0 = pool.protocol_fees_token_0;
        let actual_protocol_1 = pool.protocol_fees_token_1;
        let actual_fund_0 = pool.fund_fees_token_0;
        let actual_fund_1 = pool.fund_fees_token_1;
        let actual_dynamic_info = pool.dynamic_fee_info;
        let actual_dynamic_tick_reference = actual_dynamic_info.tick_spacing_index_reference;
        let actual_dynamic_volatility_reference = actual_dynamic_info.volatility_reference;
        let actual_dynamic_volatility_accumulator = actual_dynamic_info.volatility_accumulator;
        let actual_dynamic_timestamp = actual_dynamic_info.last_update_timestamp;
        let expected = &case["expected"];

        assert_eq!(actual_tick, expected["tick_current"].as_i64().unwrap() as i32);
        assert_eq!(actual_sqrt_price, parse_u128(&expected["sqrt_price_x64"]));
        assert_eq!(actual_liquidity, parse_u128(&expected["liquidity"]));
        assert_eq!(actual_fee_growth_0, parse_u128(&expected["fee_growth_global_0_x64"]));
        assert_eq!(actual_fee_growth_1, parse_u128(&expected["fee_growth_global_1_x64"]));
        assert_eq!(actual_protocol_0, parse_u64(&expected["protocol_fees_token_0"]));
        assert_eq!(actual_protocol_1, parse_u64(&expected["protocol_fees_token_1"]));
        assert_eq!(actual_fund_0, parse_u64(&expected["fund_fees_token_0"]));
        assert_eq!(actual_fund_1, parse_u64(&expected["fund_fees_token_1"]));
        assert_eq!(actual_dynamic_tick_reference, expected["dynamic_tick_reference"].as_i64().unwrap() as i32);
        assert_eq!(actual_dynamic_volatility_reference, expected["dynamic_volatility_reference"].as_u64().unwrap() as u32);
        assert_eq!(actual_dynamic_volatility_accumulator, expected["dynamic_volatility_accumulator"].as_u64().unwrap() as u32);
        assert_eq!(actual_dynamic_timestamp, expected["dynamic_last_update_timestamp"].as_u64().unwrap());
    }
}
