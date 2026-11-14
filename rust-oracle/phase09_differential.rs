//! Differential assertions against the exact upstream Raydium CLMM Rust source.
//! The Python workflow exporter writes the same input vectors used by the Python implementation.
use raydium_clmm::libraries::{liquidity_math, swap_math, tick_math};
use raydium_clmm::instructions::swap::{swap_internal, SwapState};
use std::cell::RefCell;
use std::collections::VecDeque;
use raydium_clmm::states::{tick_spacing_index_from_tick, AmmConfig, DynamicFeeInfo, PoolState, RewardInfo, TickArrayBitmapExtension, TickArrayState, TickState, ObservationState};
use anchor_lang::prelude::{Clock, Pubkey, Sysvar};
use anchor_lang::Discriminator;
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

fn parse_i128(value: &Value) -> i128 {
    value
        .as_str()
        .expect("i128 vector values are encoded as decimal strings")
        .parse::<i128>()
        .expect("valid i128 decimal")
}

fn parse_pubkey(value: &Value) -> Pubkey {
    value
        .as_str()
        .expect("public key string")
        .parse::<Pubkey>()
        .expect("valid public key")
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


#[test]
fn python_default_tick_array_bitmap_vectors_match_pinned_raydium_pool_state() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["tick_array_bitmap"].as_array().expect("default bitmap vectors");
    assert_eq!(cases.len(), 11, "five default-bitmap and six extension-bitmap fixtures are required");

    for case in cases.iter().take(5) {
        let spacing = case["tick_spacing"].as_u64().unwrap() as u16;
        let start_index = case["start_index"].as_i64().unwrap() as i32;
        let mut pool = PoolState::default();
        pool.tick_spacing = spacing;
        pool.flip_tick_array_bit(None, start_index)
            .unwrap_or_else(|error| panic!("Rust bitmap flip failed at {start_index}: {error:?}"));
        let actual_bitmap = pool.tick_array_bitmap;
        let expected = case["expected_bitmap"].as_array().unwrap();
        for index in 0..16 {
            let actual_word = actual_bitmap[index];
            assert_eq!(
                actual_word,
                expected[index].as_u64().unwrap(),
                "bitmap word {index} mismatch at tick-array start {start_index}"
            );
        }
        assert_eq!(
            TickArrayState::tick_count(spacing),
            60 * spacing as i32,
            "tick-array size mismatch for spacing {spacing}"
        );

        pool.flip_tick_array_bit(None, start_index)
            .unwrap_or_else(|error| panic!("Rust bitmap clear failed at {start_index}: {error:?}"));
        let cleared_bitmap = pool.tick_array_bitmap;
        assert!(cleared_bitmap.iter().all(|word| *word == 0));
    }
}


#[test]
fn python_positive_negative_bitmap_extension_vectors_match_pinned_raydium_extension() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["tick_array_bitmap"].as_array().expect("bitmap vectors");
    assert_eq!(cases.len(), 11);

    for case in cases.iter().skip(5) {
        let spacing = case["tick_spacing"].as_u64().unwrap() as u16;
        let start_index = case["start_index"].as_i64().unwrap() as i32;
        let mut extension = TickArrayBitmapExtension::default();
        extension.initialize(Pubkey::new_unique());
        extension
            .flip_tick_array_bit(start_index, spacing)
            .unwrap_or_else(|error| panic!("Rust extension bitmap flip failed at {start_index}: {error:?}"));

        let actual_positive = extension.positive_tick_array_bitmap;
        let actual_negative = extension.negative_tick_array_bitmap;
        let expected_positive = case["extension_positive"].as_array().unwrap();
        let expected_negative = case["extension_negative"].as_array().unwrap();
        for row in 0..14 {
            for word in 0..8 {
                let actual_positive_word = actual_positive[row][word];
                let actual_negative_word = actual_negative[row][word];
                assert_eq!(
                    actual_positive_word,
                    expected_positive[row][word].as_u64().unwrap(),
                    "positive bitmap mismatch at start={start_index}, row={row}, word={word}"
                );
                assert_eq!(
                    actual_negative_word,
                    expected_negative[row][word].as_u64().unwrap(),
                    "negative bitmap mismatch at start={start_index}, row={row}, word={word}"
                );
            }
        }
    }
}


fn observation_state_from_initial(initial: &Value) -> ObservationState {
    let mut state = ObservationState::default();
    state.initialized = initial["initialized"].as_bool().expect("initialized flag");
    state.recent_epoch = initial["recent_epoch"].as_u64().expect("recent epoch");
    state.observation_index = initial["observation_index"].as_u64().expect("observation index") as u16;
    state.pool_id = initial["pool_id"]
        .as_str()
        .expect("pool public key")
        .parse::<Pubkey>()
        .expect("valid pool public key");
    for observation in initial["observations"].as_array().expect("initial observations") {
        let index = observation["index"].as_u64().expect("observation slot") as usize;
        state.observations[index].block_timestamp =
            observation["block_timestamp"].as_u64().expect("observation timestamp") as u32;
        state.observations[index].tick_cumulative =
            observation["tick_cumulative"].as_i64().expect("observation cumulative");
    }
    state
}

fn assert_observation_snapshot(state: &ObservationState, expected: &Value, name: &str) {
    let actual_initialized = state.initialized;
    let actual_recent_epoch = state.recent_epoch;
    let actual_observation_index = state.observation_index;
    let actual_pool_id = state.pool_id;
    assert_eq!(
        actual_initialized,
        expected["initialized"].as_bool().expect("expected initialized"),
        "initialized mismatch for {name}"
    );
    if let Some(expected_recent_epoch) = expected["recent_epoch"].as_u64() {
        assert_eq!(
            actual_recent_epoch,
            expected_recent_epoch,
            "recent epoch mismatch for {name}"
        );
    }
    assert_eq!(
        actual_observation_index,
        expected["observation_index"].as_u64().expect("expected observation index") as u16,
        "observation index mismatch for {name}"
    );
    if let Some(pool_id) = expected["pool_id"].as_str() {
        assert_eq!(actual_pool_id, pool_id.parse::<Pubkey>().expect("expected pool public key"));
    }
    for observation in expected["observations"].as_array().expect("expected observations") {
        let index = observation["index"].as_u64().expect("expected observation slot") as usize;
        let actual = state.observations[index];
        let actual_timestamp = actual.block_timestamp;
        let actual_cumulative = actual.tick_cumulative;
        assert_eq!(
            actual_timestamp,
            observation["block_timestamp"].as_u64().expect("expected timestamp") as u32,
            "observation timestamp mismatch for {name} at slot {index}"
        );
        assert_eq!(
            actual_cumulative,
            observation["tick_cumulative"].as_i64().expect("expected cumulative"),
            "observation cumulative mismatch for {name} at slot {index}"
        );
    }
}

#[test]
fn python_observation_update_vectors_match_pinned_raydium_rust() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let cases = vectors["observation_updates"]
        .as_array()
        .expect("Python ObservationState update vectors");
    assert_eq!(cases.len(), 4);

    for case in cases {
        let name = case["name"].as_str().expect("scenario name");
        let mut state = observation_state_from_initial(&case["initial"]);
        assert_observation_snapshot(&state, &case["initial"], name);

        for update in case["updates"].as_array().expect("ObservationState updates") {
            let timestamp = update["block_timestamp"].as_u64().expect("block timestamp") as u32;
            let tick = update["tick"].as_i64().expect("tick") as i32;
            state.update(timestamp, tick);
            assert_observation_snapshot(&state, &update["expected"], name);
        }
    }
}


fn assert_account_bytes_equal(actual: &[u8], expected: &[u8], account: &str) {
    assert_eq!(actual.len(), expected.len(), "{account} account byte length mismatch");
    if let Some(offset) = actual.iter().zip(expected).position(|(left, right)| left != right) {
        let start = offset.saturating_sub(8);
        let end = (offset + 16).min(actual.len());
        panic!(
            "{account} account byte mismatch at offset {offset}: actual={:?}, expected={:?}",
            &actual[start..end],
            &expected[start..end]
        );
    }
}

fn decode_hex_bytes(value: &str) -> Vec<u8> {
    assert_eq!(value.len() % 2, 0, "hex payload must have an even length");
    (0..value.len())
        .step_by(2)
        .map(|offset| {
            u8::from_str_radix(&value[offset..offset + 2], 16)
                .expect("valid hexadecimal byte")
        })
        .collect()
}

#[test]
fn python_observation_account_serialization_matches_pinned_raydium_bytes() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let vector = &vectors["observation_serialization"];
    let state = observation_state_from_initial(&vector["state"]);
    assert_observation_snapshot(&state, &vector["state"], "serialization fixture");

    let mut rust_bytes = Vec::with_capacity(ObservationState::LEN);
    rust_bytes.extend_from_slice(ObservationState::DISCRIMINATOR);
    rust_bytes.extend_from_slice(bytemuck::bytes_of(&state));
    assert_eq!(rust_bytes.len(), ObservationState::LEN);
    let python_bytes = decode_hex_bytes(
        vector["serialized_hex"].as_str().expect("Python serialized hex"),
    );
    assert_eq!(
        rust_bytes.len(),
        vector["serialized_length"].as_u64().expect("serialized length") as usize
    );
    assert_eq!(
        rust_bytes,
        python_bytes,
        "ObservationState account bytes differ from Python serializer"
    );
}


#[test]
fn python_pool_state_account_serialization_matches_pinned_raydium_bytes() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let vector = &vectors["account_serialization"]["pool_state"];
    let mut state = PoolState::default();
    state.bump = [vector["bump"].as_u64().expect("pool bump") as u8];
    state.amm_config = parse_pubkey(&vector["amm_config"]);
    state.owner = parse_pubkey(&vector["pool_creator"]);
    state.token_mint_0 = parse_pubkey(&vector["token_mint_0"]);
    state.token_mint_1 = parse_pubkey(&vector["token_mint_1"]);
    state.token_vault_0 = parse_pubkey(&vector["token_vault_0"]);
    state.token_vault_1 = parse_pubkey(&vector["token_vault_1"]);
    state.observation_key = parse_pubkey(&vector["observation_key"]);
    state.mint_decimals_0 = vector["mint_decimals_0"].as_u64().unwrap() as u8;
    state.mint_decimals_1 = vector["mint_decimals_1"].as_u64().unwrap() as u8;
    state.tick_spacing = vector["tick_spacing"].as_u64().unwrap() as u16;
    state.liquidity = parse_u128(&vector["liquidity"]);
    state.sqrt_price_x64 = parse_u128(&vector["sqrt_price_x64"]);
    state.tick_current = vector["tick_current"].as_i64().unwrap() as i32;
    state.fee_growth_global_0_x64 = parse_u128(&vector["fee_growth_global_0_x64"]);
    state.fee_growth_global_1_x64 = parse_u128(&vector["fee_growth_global_1_x64"]);
    state.protocol_fees_token_0 = parse_u64(&vector["protocol_fees_token_0"]);
    state.protocol_fees_token_1 = parse_u64(&vector["protocol_fees_token_1"]);
    state.status = vector["status"].as_u64().unwrap() as u8;
    state.fee_on = vector["fee_on"].as_u64().unwrap() as u8;
    state.seed_index = vector["seed_index"].as_u64().unwrap().to_le_bytes()[..2]
        .try_into()
        .expect("u16 seed index bytes");
    state.fund_fees_token_0 = parse_u64(&vector["fund_fees_token_0"]);
    state.fund_fees_token_1 = parse_u64(&vector["fund_fees_token_1"]);
    for (index, word) in vector["tick_array_bitmap"].as_array().unwrap().iter().enumerate() {
        state.tick_array_bitmap[index] = word.as_u64().expect("bitmap word");
    }
    let mut rewards = std::array::from_fn(|_| RewardInfo::default());
    for (index, reward) in vector["reward_infos"].as_array().unwrap().iter().enumerate() {
        rewards[index].reward_state = reward["reward_state"].as_u64().unwrap() as u8;
        rewards[index].open_time = parse_u64(&reward["open_time"]);
        rewards[index].end_time = parse_u64(&reward["end_time"]);
        rewards[index].last_update_time = parse_u64(&reward["last_update_time"]);
        rewards[index].emissions_per_second_x64 = parse_u128(&reward["emissions_per_second_x64"]);
        rewards[index].reward_total_emitted = parse_u64(&reward["reward_total_emitted"]);
        rewards[index].reward_claimed = parse_u64(&reward["reward_claimed"]);
        rewards[index].token_mint = parse_pubkey(&reward["token_mint"]);
        rewards[index].token_vault = parse_pubkey(&reward["token_vault"]);
        rewards[index].authority = parse_pubkey(&reward["authority"]);
        rewards[index].reward_growth_global_x64 = parse_u128(&reward["reward_growth_global_x64"]);
    }
    state.reward_infos = rewards;
    state.dynamic_fee_info = dynamic_info(&vector["dynamic_fee_info"]);

    let mut rust_bytes = Vec::with_capacity(PoolState::LEN);
    rust_bytes.extend_from_slice(PoolState::DISCRIMINATOR);
    rust_bytes.extend_from_slice(bytemuck::bytes_of(&state));
    let python_bytes = decode_hex_bytes(vector["serialized_hex"].as_str().unwrap());
    assert_eq!(rust_bytes.len(), vector["serialized_length"].as_u64().unwrap() as usize);
    assert_eq!(rust_bytes.len(), PoolState::LEN);
    assert_eq!(rust_bytes, python_bytes, "PoolState bytes differ from Python serializer");
}

#[test]
fn python_tick_array_state_account_serialization_matches_pinned_raydium_bytes() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let vector = &vectors["account_serialization"]["tick_array_state"];
    let mut ticks = std::array::from_fn(|_| TickState::default());
    for (index, tick) in vector["ticks"].as_array().unwrap().iter().enumerate() {
        let mut state = TickState::default();
        state.tick = tick["tick_index"].as_i64().unwrap() as i32;
        state.liquidity_net = parse_i128(&tick["liquidity_net"]);
        state.liquidity_gross = parse_u128(&tick["liquidity_gross"]);
        state.fee_growth_outside_0_x64 = parse_u128(&tick["fee_growth_outside_0_x64"]);
        state.fee_growth_outside_1_x64 = parse_u128(&tick["fee_growth_outside_1_x64"]);
        let rewards = tick["reward_growths_outside_x64"].as_array().unwrap();
        state.reward_growths_outside_x64 = [
            parse_u128(&rewards[0]),
            parse_u128(&rewards[1]),
            parse_u128(&rewards[2]),
        ];
        state.order_phase = parse_u64(&tick["order_phase"]);
        state.orders_amount = parse_u64(&tick["orders_amount"]);
        state.part_filled_orders_remaining = parse_u64(&tick["part_filled_orders_remaining"]);
        state.unfilled_ratio_x64 = parse_u128(&tick["unfilled_ratio_x64"]);
        ticks[index] = state;
    }
    let padding_bytes = decode_hex_bytes(vector["padding_hex"].as_str().unwrap());
    let mut padding = [0u8; 107];
    padding.copy_from_slice(&padding_bytes);
    let state = TickArrayState {
        pool_id: parse_pubkey(&vector["pool_id"]),
        start_tick_index: vector["start_tick_index"].as_i64().unwrap() as i32,
        ticks,
        initialized_tick_count: vector["initialized_tick_count"].as_u64().unwrap() as u8,
        recent_epoch: parse_u64(&vector["recent_epoch"]),
        padding,
    };
    let mut rust_bytes = Vec::with_capacity(TickArrayState::LEN);
    rust_bytes.extend_from_slice(TickArrayState::DISCRIMINATOR);
    rust_bytes.extend_from_slice(bytemuck::bytes_of(&state));
    let python_bytes = decode_hex_bytes(vector["serialized_hex"].as_str().unwrap());
    assert_eq!(rust_bytes.len(), vector["serialized_length"].as_u64().unwrap() as usize);
    assert_eq!(rust_bytes.len(), TickArrayState::LEN);
    assert_eq!(rust_bytes, python_bytes, "TickArrayState bytes differ from Python serializer");
}

#[test]
fn python_tick_array_bitmap_extension_serialization_matches_pinned_raydium_bytes() {
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let vector = &vectors["account_serialization"]["tick_array_bitmap_extension"];
    let mut positive = [[0u64; 8]; 14];
    let mut negative = [[0u64; 8]; 14];
    for row in 0..14 {
        for word in 0..8 {
            positive[row][word] = vector["positive_bitmaps"][row][word].as_u64().unwrap();
            negative[row][word] = vector["negative_bitmaps"][row][word].as_u64().unwrap();
        }
    }
    let state = TickArrayBitmapExtension {
        pool_id: parse_pubkey(&vector["pool_id"]),
        positive_tick_array_bitmap: positive,
        negative_tick_array_bitmap: negative,
    };
    let mut rust_bytes = Vec::with_capacity(TickArrayBitmapExtension::LEN);
    rust_bytes.extend_from_slice(TickArrayBitmapExtension::DISCRIMINATOR);
    rust_bytes.extend_from_slice(bytemuck::bytes_of(&state));
    let python_bytes = decode_hex_bytes(vector["serialized_hex"].as_str().unwrap());
    assert_eq!(rust_bytes.len(), vector["serialized_length"].as_u64().unwrap() as usize);
    assert_eq!(rust_bytes.len(), TickArrayBitmapExtension::LEN);
    assert_eq!(
        rust_bytes,
        python_bytes,
        "TickArrayBitmapExtension bytes differ from Python serializer"
    );
}


#[test]
fn python_single_array_swap_replay_matches_pinned_raydium_swap_internal() {
    let runtime_epoch = Clock::get().expect("Clock sysvar available in pinned Rust harness").epoch;
    let vectors: Value = serde_json::from_str(PYTHON_VECTORS).expect("valid Python vector JSON");
    let fixture = &vectors["swap_replay"];
    let pool_bytes = decode_hex_bytes(fixture["initial_pool_hex"].as_str().unwrap());
    let tick_array_bytes = decode_hex_bytes(fixture["initial_tick_array_hex"].as_str().unwrap());
    let observation_bytes = decode_hex_bytes(fixture["initial_observation_hex"].as_str().unwrap());
    assert_eq!(pool_bytes.len(), PoolState::LEN);
    assert_eq!(tick_array_bytes.len(), TickArrayState::LEN);
    assert_eq!(observation_bytes.len(), ObservationState::LEN);
    assert_eq!(&pool_bytes[..8], PoolState::DISCRIMINATOR);
    assert_eq!(&tick_array_bytes[..8], TickArrayState::DISCRIMINATOR);
    assert_eq!(&observation_bytes[..8], ObservationState::DISCRIMINATOR);

    let pool_state = bytemuck::pod_read_unaligned::<PoolState>(&pool_bytes[8..]);
    let tick_array_state =
        bytemuck::pod_read_unaligned::<TickArrayState>(&tick_array_bytes[8..]);
    let observation_state =
        bytemuck::pod_read_unaligned::<ObservationState>(&observation_bytes[8..]);

    let amm_config = AmmConfig {
        trade_fee_rate: fixture["trade_fee_rate"].as_u64().unwrap() as u32,
        protocol_fee_rate: fixture["protocol_fee_rate"].as_u64().unwrap() as u32,
        fund_fee_rate: fixture["fund_fee_rate"].as_u64().unwrap() as u32,
        tick_spacing: 10,
        ..AmmConfig::default()
    };

    let pool_cell = RefCell::new(pool_state);
    let tick_array_cell = RefCell::new(tick_array_state);
    let observation_cell = RefCell::new(observation_state);
    let mut pool_ref = pool_cell.borrow_mut();
    let mut tick_array_queue = VecDeque::new();
    tick_array_queue.push_back(tick_array_cell.borrow_mut());
    let mut observation_ref = observation_cell.borrow_mut();

    let result = swap_internal(
        &amm_config,
        &mut pool_ref,
        &mut tick_array_queue,
        &mut observation_ref,
        None,
        fixture["amount_specified"].as_u64().unwrap(),
        parse_u128(&fixture["sqrt_price_limit_x64"]),
        fixture["zero_for_one"].as_bool().unwrap(),
        fixture["is_base_input"].as_bool().unwrap(),
        fixture["block_timestamp"].as_u64().unwrap() as u32,
    )
    .expect("pinned Raydium swap_internal execution");

    let expected = &fixture["expected"];
    assert_eq!(result.amount_0, expected["amount_0"].as_u64().unwrap());
    assert_eq!(result.amount_1, expected["amount_1"].as_u64().unwrap());
    assert_eq!(result.trade_fee_0, expected["trade_fee_0"].as_u64().unwrap());
    assert_eq!(result.trade_fee_1, expected["trade_fee_1"].as_u64().unwrap());
    assert_eq!(result.tick, expected["tick"].as_i64().unwrap() as i32);
    assert_eq!(result.sqrt_price_x64, parse_u128(&expected["sqrt_price_x64"]));
    assert_eq!(result.liquidity, parse_u128(&expected["liquidity"]));

    let mut final_pool_bytes = PoolState::DISCRIMINATOR.to_vec();
    final_pool_bytes.extend_from_slice(bytemuck::bytes_of(&*pool_ref));
    let mut final_tick_array_bytes = TickArrayState::DISCRIMINATOR.to_vec();
    {
        let final_tick_array = tick_array_cell.borrow();
        final_tick_array_bytes.extend_from_slice(bytemuck::bytes_of(&*final_tick_array));
    }
    let mut final_observation_bytes = ObservationState::DISCRIMINATOR.to_vec();
    final_observation_bytes.extend_from_slice(bytemuck::bytes_of(&*observation_ref));

    let mut expected_pool_bytes = decode_hex_bytes(expected["pool_hex"].as_str().unwrap());
    let mut expected_tick_array_bytes = decode_hex_bytes(expected["tick_array_hex"].as_str().unwrap());
    // swap_internal refreshes both recent_epoch fields from Clock::get(); the
    // Python fixture uses epoch zero, so normalize only these two environmental
    // u64 fields to the epoch observed by the pinned Rust harness.
    expected_pool_bytes[1088..1096].copy_from_slice(&runtime_epoch.to_le_bytes());
    expected_tick_array_bytes[10125..10133].copy_from_slice(&runtime_epoch.to_le_bytes());
    assert_account_bytes_equal(
        &final_pool_bytes,
        &expected_pool_bytes,
        "final PoolState",
    );
    assert_account_bytes_equal(
        &final_tick_array_bytes,
        &expected_tick_array_bytes,
        "final TickArrayState",
    );
    assert_account_bytes_equal(
        &final_observation_bytes,
        &decode_hex_bytes(expected["observation_hex"].as_str().unwrap()),
        "final ObservationState",
    );
}
